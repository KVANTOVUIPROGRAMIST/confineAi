import stripe
from fastapi import HTTPException
from sqlalchemy import select, update

from .config import settings
from .models import BillingEvent, User


def checkout(db, user, topup=False):
    if not settings.billing_ready:
        raise HTTPException(503, 'Payments are disabled during the free beta.')
    stripe.api_key = settings.stripe_key
    if not user.stripe_customer:
        customer = stripe.Customer.create(email=user.email, metadata={'user_id': user.id},
                                          idempotency_key=f'customer-{user.id}')
        user.stripe_customer = customer.id
        db.commit()
    price = settings.stripe_topup if topup else settings.stripe_price
    if not price:
        raise HTTPException(503, 'Top-ups have not been configured yet.')
    if not topup and user.tier == 'student':
        return portal(user)
    session = stripe.checkout.Session.create(customer=user.stripe_customer,
        mode='payment' if topup else 'subscription', line_items=[{'price': price, 'quantity': 1}],
        metadata={'user_id': user.id, 'purchase': 'topup' if topup else 'student'},
        **({'subscription_data': {'metadata': {'user_id': user.id}}} if not topup else {}),
        success_url=settings.app_url + '/?payment=success', cancel_url=settings.app_url + '/?payment=cancelled')
    return {'url': session.url}


def portal(user):
    if not settings.billing_ready or not user.stripe_customer:
        raise HTTPException(503, 'No paid billing account is available during this beta.')
    stripe.api_key = settings.stripe_key
    session = stripe.billing_portal.Session.create(customer=user.stripe_customer, return_url=settings.app_url)
    return {'url': session.url}


def handle_event(db, event):
    """Signed events are fulfilled atomically with their deduplication record."""
    if db.get(BillingEvent, event['id']):
        return
    obj = event['data']['object']
    user = db.scalar(select(User).where(User.stripe_customer == obj.get('customer'))) if obj.get('customer') else None
    db.add(BillingEvent(id=event['id']))
    try:
        db.flush()
        if user and event['type'] == 'invoice.paid':
            # Only the configured student price earns the monthly allowance.
            lines = obj.get('lines', {}).get('data', [])
            matched = [line for line in lines if (line.get('price', {}).get('id') if isinstance(line.get('price'), dict) else None) == settings.stripe_price
                       or line.get('pricing', {}).get('price_details', {}).get('price') == settings.stripe_price]
            # Plan changes / incidental invoices must not reset remaining credits.
            if matched and obj.get('billing_reason') in ('subscription_create', 'subscription_cycle'):
                period = str(matched[0].get('period', {}).get('start', obj.get('period_start', '')))
                subscription = obj.get('subscription') or obj.get('parent', {}).get('subscription_details', {}).get('subscription')
                if period and user.period != period:
                    user.tier, user.credits, user.period = 'student', 300, period
                    user.subscription_id = subscription
        elif user and event['type'] in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            if obj.get('mode') == 'payment' and obj.get('payment_status') == 'paid' and obj.get('metadata', {}).get('purchase') == 'topup':
                # Deduplicate across completed/async events using the Checkout Session itself.
                purchase_key = 'purchase-' + obj['id']
                if not db.get(BillingEvent, purchase_key):
                    db.add(BillingEvent(id=purchase_key))
                    user.topup_credits += 100
        elif user and event['type'] == 'customer.subscription.deleted':
            if user.subscription_id == obj.get('id'):
                user.tier, user.credits, user.subscription_id = 'free', 0, None
        db.commit()
    except Exception:
        db.rollback()
        if db.get(BillingEvent, event['id']):
            return
        raise
