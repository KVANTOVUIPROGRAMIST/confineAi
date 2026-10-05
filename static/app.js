const $ = (selector, root = document) => root.querySelector(selector);
const escape = (value = '') => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
  home:'<path d="m3 10 7-7 7 7v7H3z"/><path d="M8 17v-5h4v5"/>',
  chat:'<path d="M3 4h14v10H8l-5 3z"/><path d="M6 8h8M6 11h5"/>',
  file:'<path d="M5 2h7l4 4v12H5z"/><path d="M12 2v5h4M8 11h5M8 14h5"/>',
  books:'<path d="M3 3h4v14H3zM9 3h4v14H9zM14 5l3-1 3 12-3 1z"/>',
  chart:'<path d="M3 17h15M5 13V9M10 13V4M15 13V7"/>',
  settings:'<path d="m8 2-1 3-3 1 1 3-1 3 3 1 1 3h3l1-3 3-1-1-3 1-3-3-1-1-3z"/><circle cx="9.5" cy="9" r="2.5"/>',
  plus:'<path d="M10 4v12M4 10h12"/>',
  arrow:'<path d="M4 10h12m-5-5 5 5-5 5"/>',
  down:'<path d="m5 8 5 5 5-5"/>',
  check:'<path d="m4 10 4 4 8-9"/>',
  shield:'<path d="m10 2 7 3v5c0 4-7 8-7 8s-7-4-7-8V5z"/><path d="m6 10 3 3 5-6"/>',
  search:'<circle cx="8" cy="8" r="5"/><path d="m12 12 5 5"/>',
  upload:'<path d="M10 13V3m-4 4 4-4 4 4M3 12v5h14v-5"/>',
  download:'<path d="M10 3v10m-4-4 4 4 4-4M3 13v4h14v-4"/>',
  info:'<circle cx="10" cy="10" r="7"/><path d="M10 9v5M10 6v.1"/>',
  close:'<path d="m5 5 10 10M15 5 5 15"/>',
  logout:'<path d="M8 3H3v14h5M7 10h10m-4-4 4 4-4 4"/>',
  code:'<path d="m6 5-4 5 4 5m8-10 4 5-4 5M12 3l-4 14"/>',
  math:'<path d="m3 12 3 4 5-12h7M14 9l4 5M18 9l-4 5"/>',
  globe:'<circle cx="10" cy="10" r="7"/><path d="M3 10h14M10 3c5 4 5 10 0 14-5-4-5-10 0-14"/>',
  clock:'<circle cx="10" cy="10" r="7"/><path d="M10 6v4l3 2"/>',
  menu:'<path d="M3 5h14M3 10h14M3 15h14"/>',
  trash:'<path d="M3 5h14M6 5V3h8v2M5 5l1 12h8l1-12M8 8v6M12 8v6"/>',
};
const icon = name => `<svg viewBox="0 0 20 20" aria-hidden="true">${icons[name] || icons.file}</svg>`;
const state = {config:null,user:null,courses:[],featured:[],page:'home',selected:null,chats:[],materials:[],sources:[],assignments:[],usage:null,search:'',school:'ucsd',modal:null,busy:false};
let toastTimer, searchTimer, pollTimer, modalFocus;

async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (state.user?.csrf) headers['X-CSRF-Token'] = state.user.csrf;
  const response = await fetch('/api' + path, {...options, headers, credentials:'same-origin'});
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    const message = typeof data?.detail === 'string' ? data.detail : Array.isArray(data?.detail) ? data.detail.map(e => e.msg).join('; ') : 'The request could not finish. Please try again.';
    throw Object.assign(new Error(message), {status:response.status});
  }
  return data;
}
const post = (path, body) => api(path, {method:'POST',body:JSON.stringify(body)});
const key = () => crypto.randomUUID().replaceAll('-', '');
function toast(message) { const el=$('#toast'); el.textContent=message; el.classList.add('show'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove('show'),6000); }
function initials(name='Student') { return name.split(/\s+/).slice(0,2).map(n=>n[0]).join('').toUpperCase(); }
function currentCourse() { return state.courses.find(c=>c.id===state.selected) || state.courses[0]; }
function courseIcon(course) { return course.code.startsWith('MATH') ? 'math' : course.code.startsWith('MMW') ? 'globe' : 'code'; }
function courseColor(course) { return course.code.startsWith('MATH') ? 'blue' : course.code.startsWith('MMW') ? 'peach' : ''; }
function courseOptions() { return state.courses.map(c=>`<option value="${c.id}" ${c.id===currentCourse()?.id?'selected':''}>${escape(c.course.code)} · ${escape(c.course.title)}</option>`).join(''); }
function notice() { return !state.config.ai_ready ? `<div class="notice amber">${icon('info')}<div><strong>Your workspace is ready. Live AI needs a connection.</strong><br/>Select classes, add materials, and preview assignments now. An API key enables tutoring and completed solution files. <button class="text-button" data-action="nav" data-page="settings">View setup ${icon('arrow')}</button></div></div>` : ''; }

function sidebar() {
  const items=[['home','home','My workspace'],['tutor','chat','Course tutor'],['assignments','file','Assignments'],['catalog','books','Course library'],['usage','chart','Usage & plan']];
  const remaining = (state.user?.credits || 0)+(state.user?.topup_credits || 0);
  return `<aside class="sidebar"><button class="nav-mobile-close" data-action="mobile-close" aria-label="Close navigation">${icon('close')}</button>
    <a href="/" class="brand"><img src="/static/favicon.svg" alt=""/>confine<small>BETA</small></a>
    <div class="nav-label">YOUR STUDY SPACE</div><nav class="nav" aria-label="Main navigation">${items.map(([p,i,label])=>`<button data-action="nav" data-page="${p}" class="${state.page===p?'active':''}" ${state.page===p?'aria-current="page"':''}>${icon(i)}${label}${p==='assignments'&&state.assignments.length?`<span class="nav-count">${state.assignments.length}</span>`:''}</button>`).join('')}</nav>
    <div class="sidebar-bottom"><div class="allowance-card"><div class="row"><span>${state.user?'Free beta allowance':'Start with the free beta'}</span>${icon('shield')}</div><strong>${state.user?remaining+' responses left':state.config.beta_credits+' responses / month'}</strong><progress max="${Math.max(state.config.beta_credits,remaining)}" value="${state.user?remaining:state.config.beta_credits}" aria-label="Remaining response allowance"></progress><p>${state.user?'Unsupported answers restore your allowance.':'A little more clarity. A lot less friction.'}</p><button data-action="${state.user?'nav':'auth'}" data-page="usage">${state.user?'View usage & plan':'Create your workspace'}</button></div>
    <div class="user-row"><div class="avatar">${escape(initials(state.user?.name))}</div><div><strong>${escape(state.user?.name || 'Your workspace')}</strong><span>${state.user?'UC San Diego · Free beta':'No account required to browse'}</span></div><button data-action="${state.user?'nav':'auth'}" data-page="settings" aria-label="${state.user?'Account settings':'Sign in'}">${icon('settings')}</button></div></div></aside>`;
}

function render() {
  const labels={home:'My workspace',tutor:'Course tutor',assignments:'Assignments',catalog:'Course library',usage:'Usage & plan',settings:'Settings'};
  $('#app').innerHTML=`<div class="shell">${sidebar()}<main class="main"><header class="topbar"><div class="breadcrumb"><button class="icon-button mobile-menu" data-action="mobile" aria-label="Open navigation">${icon('menu')}</button>${icon('home')}<span>Workspace</span><span>/</span><span>${labels[state.page]}</span></div><div class="top-actions"><span class="pill">${state.config.beta?'Free beta':'Student workspace'}</span>${state.user?`<div class="avatar">${escape(initials(state.user.name))}</div>`:`<button class="btn secondary small" data-action="auth" data-mode="login">Sign in</button>`}</div></header><div id="page-content">${({home:homePage,tutor:tutorPage,assignments:assignmentsPage,catalog:catalogPage,usage:usagePage,settings:settingsPage}[state.page])()}</div></main></div>`;
  if(state.page==='catalog') loadCatalog();
  if(state.page==='tutor') requestAnimationFrame(()=>{ const messages=$('.messages'); if(messages)messages.scrollTop=messages.scrollHeight; });
}

function courseCard(item, enrolled=false) {
  const c=enrolled?item.course:item;
  return `<button class="course-card" data-action="${enrolled?'open-course':'add-course'}" data-id="${item.id}"><div class="course-top"><span class="course-icon ${courseColor(c)}">${icon(courseIcon(c))}</span><span class="eyebrow muted">UCSD</span></div><div class="course-code">${escape(c.code)}</div><h3>${escape(c.title)}</h3><div class="course-meta">${enrolled?`<span>${item.material_count} material${item.material_count===1?'':'s'}</span><span>${item.mode==='strict'?'Class materials':'Catalog + references'}</span>`:`<span>2026–27 catalog</span><span>${c.prerequisite_codes.length?'Prerequisites on file':'Select to set up'}</span>`}</div></button>`;
}

function homePage() {
  const name=state.user?.name.split(' ')[0];
  return `<div class="page-heading"><div><h1>${name?`Welcome back, ${escape(name)}.`:'Your course. Your context.'}</h1><p>A calmer place to understand what you’re learning.</p></div><button class="btn secondary" data-action="nav" data-page="catalog">${icon('plus')}Add a course</button></div>
    <section class="hero"><div class="hero-copy"><div class="eyebrow">BUILT AROUND YOUR CLASSROOM</div><h2 class="serif">A little less lost.<br/>A lot more understanding.</h2><p>Get explanations and worked solutions grounded in your course materials and selected prerequisites.</p><button class="btn" data-action="nav" data-page="${state.courses.length?'tutor':'catalog'}">${state.courses.length?'Ask your course tutor':'Find your courses'}${icon('arrow')}</button></div><div class="hero-art" aria-hidden="true"><div class="art-orbit"></div><div class="art-sheet"><div class="art-label">MATH 183 / YOUR CONTEXT</div><div class="line"></div><div class="line short"></div><div class="art-equation">P(X = k) = (ⁿₖ)pᵏ(1−p)ⁿ⁻ᵏ</div><div class="line"></div><div class="line short"></div><div class="art-label">A FORMULA. A SOURCE. A NEXT STEP.</div></div><div class="art-check">${icon('check')}Sources attached</div></div></section>
    ${state.user?notice():''}
    <div class="section-head"><div><h2>${state.courses.length?'Your courses':'Your UCSD starting set'}</h2>${!state.courses.length?'<p>Your five test courses, ready to select.</p>':''}</div><button class="text-button" data-action="${state.courses.length?'nav':'add-set'}" data-page="catalog">${state.courses.length?'Browse library':'Add all five'}${icon('arrow')}</button></div>
    <div class="course-grid">${(state.courses.length?state.courses:state.featured).map(c=>courseCard(c,!!state.courses.length)).join('')}<button class="course-card course-add" data-action="nav" data-page="catalog"><span class="course-icon">${icon('plus')}</span><h3>Add another course</h3><p>Search the UCSD catalog</p></button></div>
    <div class="bottom-grid"><section class="panel"><h3>From assignment to understanding</h3><p>Upload once. Get a document you can work through.</p><div class="workflow-list"><div class="workflow-item"><span class="number">01</span><div><strong>Upload your assignment</strong><p>PDF, text, or a photo of the questions</p></div>${icon('upload')}</div><div class="workflow-item"><span class="number">02</span><div><strong>Check the questions and course</strong><p>Your methods and prerequisites stay in scope</p></div>${icon('shield')}</div><div class="workflow-item"><span class="number">03</span><div><strong>Download the worked solutions</strong><p>Steps, answers, and evidence in one PDF</p></div>${icon('download')}</div></div><button class="text-button spaced" data-action="nav" data-page="assignments">Open assignments ${icon('arrow')}</button></section><section class="panel"><h3>What your tutor can draw from</h3><p>You stay in control of the boundaries.</p><div class="workflow-list"><div class="workflow-item"><span class="course-icon">${icon('books')}</span><div><strong>Your course materials</strong><p>Lecture notes, readings, and worked examples</p></div></div><div class="workflow-item"><span class="course-icon blue">${icon('math')}</span><div><strong>Selected prerequisites</strong><p>Reusable references, with their origin visible</p></div></div><div class="workflow-item"><span class="course-icon peach">${icon('shield')}</span><div><strong>Evidence before answers</strong><p>Missing support is flagged instead of filled in</p></div></div></div></section></div>`;
}

function catalogPage() {
  return `<div class="page-heading"><div><h1>Find your classroom.</h1><p>Course descriptions and prerequisites, already on file.</p></div><span class="pill">993 UCSD courses</span></div><div class="search-wrap"><div class="search-field">${icon('search')}<input class="field" id="catalog-search" placeholder="Search a course code or name, e.g. CSE 21" value="${escape(state.search)}" aria-label="Search course catalog"/></div><select class="field" id="school-select" aria-label="University">${state.config.schools.map(s=>`<option value="${s.id}" ${state.school===s.id?'selected':''}>${escape(s.name)}${s.available?'':' — not imported yet'}</option>`).join('')}</select></div><div class="notice">${icon('info')}<div>Catalog entries set the course scope. Uploaded materials give your tutor the formulas, examples, and methods used in your section.</div></div><div id="catalog-results" class="catalog-results"><div class="muted">Loading course library…</div></div>`;
}

async function loadCatalog() {
  const search=state.search,school=state.school;
  try {
    const courses=await api(`/catalog?school=${encodeURIComponent(school)}&q=${encodeURIComponent(search)}`);
    if(state.page!=='catalog'||search!==state.search||school!==state.school)return;
    $('#catalog-results').innerHTML=courses.length?courses.map(c=>`<article class="catalog-card"><div class="row"><strong>${escape(c.code)}</strong><span class="badge">${escape(c.catalog_year)}</span></div><h3>${escape(c.title)}</h3><p class="description">${escape(c.description)}</p><p>${c.prerequisite_codes.length?`${c.prerequisite_codes.length} prerequisite suggestions · check official requirements`:'See catalog for enrollment requirements'}</p><div class="row"><a href="${escape(c.source_url)}" target="_blank" rel="noopener noreferrer">Official catalog ↗</a><button class="btn secondary small" data-action="add-course" data-id="${c.id}">${state.courses.some(e=>e.course.id===c.id)?'Course settings':'+ Add course'}</button></div></article>`).join(''):`<div class="empty-state"><h3>${school==='ucsd'?'No matching course':'This campus is next on the list'}</h3><p>${school==='ucsd'?'Try the course code without hyphens or leading zeros. This beta includes CSE, MATH, MMW, PHYS, ECE, DSC, and COGS.':'The beta starts with UC San Diego. Other UC campuses can be added through the catalog import pipeline.'}</p></div>`;
  } catch(error) { toast(error.message); }
}

function needCourses() { return `<div class="empty-state"><span class="course-icon">${icon('books')}</span><h3>Select your course first</h3><p>Your course and prerequisites define the sources available to your tutor.</p><button class="btn" data-action="nav" data-page="catalog">Find a course ${icon('arrow')}</button></div>`; }
function sourceDetail(source) { return `<details class="source-details" id="source-${escape(source.id)}"><summary>${escape(source.name)} · ${source.origin==='upload'?`page ${source.page}`:'Original Confine reference'}</summary><p>${escape(source.text)}</p></details>`; }
function answerHTML(answer) {
  const sources=answer.sources||[];
  return `<div class="answer-body"><div class="answer-brand"><img src="/static/favicon.svg" alt=""/>Confine</div><div>${escape(answer.summary)}</div>${answer.steps.length?`<ol>${answer.steps.map(s=>`<li>${escape(s.explanation)} ${s.source_ids.map(id=>{ const source=sources.find(x=>x.id===id); return `<button class="citation" data-action="citation" data-id="${escape(id)}" title="${escape(source?.name||id)}">${escape(source?.origin==='upload'?`p. ${source.page}`:'ref')}</button>`; }).join('')}</li>`).join('')}</ol>`:''}${answer.final_answer?`<div class="answer-final">${escape(answer.final_answer)}</div>`:''}<div class="answer-status">${icon(answer.status==='answered'?'shield':'info')}${answer.status==='answered'?'Source check passed · Generated explanations can still contain errors.':'Needs supporting material · Response allowance restored.'}</div>${sources.map(sourceDetail).join('')}</div>`;
}

function tutorPage() {
  const enrollment=currentCourse();
  return `<div class="page-heading"><div><h1>Make it make sense.</h1><p>Ask a question. Keep the explanation inside your course.</p></div>${enrollment?`<select class="field course-select" id="active-course" aria-label="Active course">${courseOptions()}</select>`:''}</div>${notice()}${!enrollment?needCourses():`<div class="workspace-layout"><section class="chat-panel"><div class="chat-header row"><div><h3>${escape(enrollment.course.code)} · ${escape(enrollment.course.title)}</h3><p>${enrollment.mode==='strict'?'Class materials + selected prerequisite references':'Catalog scope + original references + your materials'}</p></div><button class="icon-button" data-action="course-settings" aria-label="Course settings">${icon('settings')}</button></div><div class="messages">${state.chats.length?state.chats.map(c=>`<div class="message"><div class="question-bubble">${escape(c.question)}</div>${answerHTML(c.answer)}</div>`).join(''):`<div class="chat-welcome"><span class="course-icon">${icon('chat')}</span><h2 class="serif">Start with what’s unclear.</h2><p>Your tutor will use the sources available for ${escape(enrollment.course.code)}. Add class notes for explanations that follow your section’s approach.</p><div class="suggestions"><button data-action="suggestion" data-question="Explain the main idea in my uploaded notes.">Explain a concept</button><button data-action="suggestion" data-question="Walk me through the worked example in my course notes, explaining each step.">Walk through an example</button><button data-action="suggestion" data-question="Explain when the formulas in my course notes apply, including their assumptions.">Understand a formula</button></div></div>`}${state.busy?'<div class="notice"><span class="spinner"></span>Finding evidence and checking the explanation…</div>':''}</div><form id="chat-form" class="composer"><div class="composer-box"><textarea id="question" placeholder="What would you like to understand?" aria-label="Question for your course tutor" maxlength="5000" ${state.busy?'disabled':''}></textarea><button class="btn" type="submit" ${state.busy||!state.config.ai_ready?'disabled':''} aria-label="Send question">${icon('arrow')}</button></div><p>${state.config.ai_ready?'One supported answer uses one response. Follow-ups count separately.':'Live answers activate after an API key is connected.'}</p></form></section>${sourcesPanel(enrollment)}</div>`}`;
}

function sourcesPanel(enrollment) {
  return `<aside class="side-panel"><section class="panel"><div class="row"><h3>Class materials</h3><span class="badge">${state.materials.length}</span></div>${state.materials.length?state.materials.map(d=>`<div class="source-row">${icon('file')}<span>${escape(d.name)}<small>${d.pages} page${d.pages===1?'':'s'} · ${Math.ceil(d.size/1024)} KB</small></span><button data-action="delete-material" data-id="${d.id}" aria-label="Remove ${escape(d.name)}">${icon('trash')}</button></div>`).join(''):'<p class="file-note">Add notes, assigned readings, or a formula sheet. Your uploads stay private to your account.</p>'}<button class="btn secondary" data-action="upload-material">${icon('plus')}Add material</button><input class="hidden" id="material-file" type="file" accept=".pdf,.txt,.md,.png,.jpg,.jpeg"/></section><section class="panel"><h3>Approved prerequisites</h3><div class="mini-list">${enrollment.prerequisites.length?enrollment.prerequisites.map(id=>`<span>${escape(id.replace('ucsd-','').replaceAll('-',' ').toUpperCase())}</span>`).join(''):'<p class="file-note">Choose the prerequisite courses you want to permit.</p>'}</div><button class="text-button spaced" data-action="course-settings">Adjust course scope ${icon('arrow')}</button></section><section class="panel"><h3>Available evidence</h3>${state.sources.length?state.sources.slice(0,8).map(s=>`<div class="source-row">${icon(s.origin==='upload'?'file':'books')}<span>${escape(s.title)}<small>${s.origin==='upload'?escape(s.name)+' · p. '+s.page:'Original reference · '+escape(s.course_code)}</small></span></div>`).join(''):'<p class="file-note">This section needs class material before answers can be grounded.</p>'}<p class="file-note">Catalog descriptions define scope. They are not evidence for detailed solutions.</p></section></aside>`;
}

function assignmentsPage() {
  const enrollment=currentCourse();
  return `<div class="page-heading"><div><h1>Your next assignment, unpacked.</h1><p>Turn the questions into a document of cited, worked solutions.</p></div>${enrollment?`<select class="field course-select" id="active-course" aria-label="Assignment course">${courseOptions()}</select>`:''}</div>${notice()}${!enrollment?needCourses():`<div class="workspace-layout"><div><section class="dropzone" id="assignment-dropzone">${icon('upload')}<h3>Drop your assignment here</h3><p>PDF, TXT, Markdown, PNG, or JPG · up to 10 MB</p><label class="btn secondary" for="assignment-file">Choose a file</label><input id="assignment-file" type="file" accept=".pdf,.txt,.md,.png,.jpg,.jpeg"/><p class="file-note">We’ll show the extracted questions before you start.</p></section><div class="section-head spaced"><h2>Saved assignments</h2><span class="muted">${state.assignments.filter(a=>a.enrollment_id===enrollment.id).length} documents</span></div><div class="assignment-list">${state.assignments.filter(a=>a.enrollment_id===enrollment.id).map(a=>`<article class="assignment-card"><span class="course-icon">${icon('file')}</span><div><h3>${escape(a.title)}</h3><p>${a.questions.length} questions · ${new Date(a.created_at).toLocaleDateString()}${['queued','running'].includes(a.status)?` · ${a.progress}/${a.questions.length} processed`:''}</p></div><div class="assignment-actions"><span class="badge ${a.status}">${escape(a.status)}</span><button class="btn secondary small" data-action="view-assignment" data-id="${a.id}">${a.status==='draft'?'Review':'View'}</button>${a.download_ready?`<a class="btn small" href="/api/assignments/${a.id}/download">${icon('download')}PDF</a>`:''}</div></article>`).join('')||'<p class="muted">Your completed documents will appear here.</p>'}</div></div><aside class="side-panel"><section class="panel"><h3>Your assignment’s boundaries</h3><div class="mini-list"><span>${escape(enrollment.course.code)} · ${enrollment.mode==='strict'?'Class materials':'Catalog + references'}</span><span>${state.materials.length} uploaded materials</span><span>${enrollment.prerequisites.length} selected prerequisites</span></div><button class="btn secondary" data-action="nav" data-page="tutor">${icon('plus')}Add supporting material</button></section><section class="panel"><h3>What’s in the download?</h3><p class="file-note">Each question, the worked steps, the final answer, and the supporting passages. Questions without enough evidence are marked clearly.</p><p class="file-note">One supported question uses one response. Unanswered questions restore their reserved responses.</p><p class="file-note">The result is a separate solution document. Original assignment layout is not preserved.</p></section></aside></div>`}`;
}

function usagePage() {
  if(!state.user)return `<div class="page-heading"><div><h1>A predictable study budget.</h1><p>Start with the free beta. Keep your usage visible.</p></div></div><div class="empty-state"><h3>${state.config.beta_credits} responses each month</h3><p>Create an account to try the free beta. Payments are currently disabled.</p><button class="btn" data-action="auth">Start free beta</button></div>`;
  const remaining=state.user.credits+state.user.topup_credits;
  const consumed=state.usage?.entries.filter(e=>e.status==='completed').reduce((n,e)=>n+e.consumed,0)||0;
  return `<div class="page-heading"><div><h1>Clarity, with a clear allowance.</h1><p>See what you’ve used and what’s available.</p></div><span class="pill">${escape(state.user.tier)} plan</span></div><div class="usage-grid"><div class="stat-card"><p>Responses available</p><strong>${remaining}</strong><span>${state.user.credits} monthly + ${state.user.topup_credits} purchased</span></div><div class="stat-card"><p>Used in recent activity</p><strong>${consumed}</strong><span>Supported answers only · latest 50 requests</span></div><div class="stat-card"><p>Monthly plan price</p><strong>${state.config.beta?'$0':'$15'}</strong><span>${state.config.beta?'Free while in beta':'300 monthly responses'}</span></div></div><div class="notice">${icon('info')}<div>${state.user.tier==='beta'?'Beta responses reset on the first day of each month (UTC). Unused monthly responses do not roll over.':'Monthly responses renew after a successful subscription payment. Purchased responses are tracked separately.'} Technical failures and answers withheld for missing evidence restore their reserved allowance. Scan transcription and question extraction don’t use student response credits in this beta.</div></div><section class="panel"><h3>Recent activity</h3><div class="table-wrap"><table><thead><tr><th>Date</th><th>Activity</th><th>Responses</th><th>Status</th></tr></thead><tbody>${state.usage?.entries.map(e=>`<tr><td>${new Date(e.created_at).toLocaleString()}</td><td>${e.operation==='chat'?'Course tutor':'Assignment'}</td><td>${e.consumed}</td><td><span class="badge ${e.status}">${escape(e.status)}</span></td></tr>`).join('')||'<tr><td colspan="4">No responses used yet.</td></tr>'}</tbody></table></div></section><section class="pricing-card"><div><h3>Student plan</h3><p>300 responses each month · cited solutions · private course materials</p><p>${state.config.beta?'Planned pricing. No payment is collected during the beta.':'Optional top-up: $5 for 100 extra responses.'}</p></div><div class="pricing-price">$15<small> / month</small></div><button class="btn ${state.config.beta?'secondary':''}" data-action="checkout" ${!state.config.billing_ready?'disabled':''}>${state.config.beta?'Coming after beta':'Choose Student'}</button>${state.config.billing_ready?'<button class="btn secondary" data-action="checkout-topup">Add 100 responses</button>':''}</section>`;
}

function settingsPage() {
  return `<div class="page-heading"><div><h1>Your workspace, configured.</h1><p>Account details and the connections behind your study tools.</p></div></div><div class="settings-grid"><section class="panel"><h3>Account</h3>${state.user?`<div class="form-grid spaced"><div><label>Name</label><p>${escape(state.user.name)}</p></div><div><label>Email</label><p>${escape(state.user.email)}</p></div><div><label>Plan</label><p>${escape(state.user.tier)} · ${state.user.credits+state.user.topup_credits} responses remaining</p></div></div><div class="row spaced"><button class="btn secondary" data-action="logout">${icon('logout')}Sign out</button>${state.config.billing_ready?'<button class="btn secondary" data-action="billing-portal">Manage subscription</button>':''}</div><button class="text-button spaced" data-action="delete-account">Delete account and saved materials</button>`:'<p class="long-copy spaced">Create your account to save courses, private materials, and completed assignments.</p><button class="btn spaced" data-action="auth">Create account</button>'}</section><section class="panel"><div class="row"><h3>AI connection</h3><span class="badge">${state.config.ai_ready?'Connected':'Not connected'}</span></div><div class="long-copy spaced"><p>${state.config.ai_ready?`The server is configured for ${escape(state.config.ai_provider)}. Tutoring requests have no web browsing tools enabled.`:'The app is ready for Gemini or OpenAI. Your server owner connects one API key; students never need their own keys.'}</p><p>For the first test, create a Gemini API key in <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener noreferrer">Google AI Studio ↗</a> and set it as <strong>GEMINI_API_KEY</strong> in the server environment. The README includes local and Render setup.</p><p>Free-tier model limits and data handling depend on the provider. <a href="https://ai.google.dev/gemini-api/docs/pricing" target="_blank" rel="noopener noreferrer">Review Gemini’s tiers ↗</a></p></div></section><section class="panel"><h3>How source restrictions work</h3><div class="long-copy spaced"><p><strong>Catalog mode</strong> uses original Confine references mapped to course topics, selected prerequisite references, and your uploads.</p><p><strong>Class materials mode</strong> uses your uploads and the prerequisite references you explicitly selected. Exact instructor alignment requires the appropriate class materials.</p><p>Every answer receives an evidence check. This reduces unsupported methods, but generated explanations can still contain errors. Confirm important work against your sources.</p></div></section><section class="panel"><h3>Privacy and beta scope</h3><div class="long-copy spaced"><p>Your materials and answers are scoped to your account. Relevant excerpts are sent to the configured AI provider when you request an answer. Scans and photos require provider transcription.</p><p>The initial catalog covers seven UCSD departments. Other UC campuses are listed for future imports. Variable-topic classes such as CSE 190 need section-specific context.</p><p>Original uploads are parsed into text passages rather than retained as downloadable originals. You can delete materials and assignment exports from your workspace.</p></div></section></div>`;
}

async function refreshUser() { if(state.user){const old=state.user.csrf;state.user=await api('/auth/me');state.user.csrf ||= old;} }
async function loadWorkspace() {
  if(!state.user)return;
  [state.courses,state.assignments]=await Promise.all([api('/courses'),api('/assignments')]);
  if(!state.courses.some(c=>c.id===state.selected))state.selected=state.courses[0]?.id||null;
}
async function loadCourseData() {
  const course=currentCourse();
  if(!course)return;
  state.selected=course.id;
  [state.chats,state.materials,state.sources]=await Promise.all([api('/chat/'+course.id),api('/materials/'+course.id),api('/sources/'+course.id)]);
}
async function navigate(page) {
  if(!state.user&&!['home','catalog','settings','usage'].includes(page)){authModal();return;}
  state.page=page;
  if(['tutor','assignments'].includes(page))await loadCourseData();
  if(page==='usage'&&state.user){state.usage=await api('/usage');await refreshUser();}
  render();
  window.scrollTo({top:0});
}

function showModal(title,subtitle,body,foot='',large=false) {
  modalFocus=document.activeElement;
  $('#modal-root').innerHTML=`<div class="modal-backdrop"><section class="modal ${large?'large':''}" role="dialog" aria-modal="true" aria-labelledby="modal-title"><header class="modal-head"><div><h2 id="modal-title">${escape(title)}</h2><p>${escape(subtitle)}</p></div><button data-action="close-modal" aria-label="Close dialog">${icon('close')}</button></header><div class="modal-body">${body}</div>${foot?`<footer class="modal-foot">${foot}</footer>`:''}</section></div>`;
  setTimeout(()=>$('.modal input,.modal textarea,.modal button')?.focus(),50);
}
function closeModal() { $('#modal-root').innerHTML='';state.modal=null;modalFocus?.focus?.(); }

function authModal(mode='register') {
  state.modal={type:'auth',mode};
  const signup=mode==='register';
  showModal(signup?'A workspace for your coursework.':'Welcome back.','Your courses, sources, and solutions in one place.',`<div class="auth-intro"><div class="brand"><img src="/static/favicon.svg" alt=""/>confine</div><p>${signup?'Start the free beta with '+state.config.beta_credits+' responses a month. No payment details needed.':'Sign in to pick up where you left off.'}</p></div><form id="auth-form" class="form-grid">${signup?'<div><label for="auth-name">Your name</label><input id="auth-name" class="field" autocomplete="name" maxlength="80" required/></div>':''}<div><label for="auth-email">Email</label><input id="auth-email" class="field" type="email" autocomplete="email" maxlength="254" required/></div><div><label for="auth-password">Password</label><input id="auth-password" class="field" type="password" autocomplete="${signup?'new-password':'current-password'}" minlength="10" maxlength="128" required/><p class="help">At least 10 characters.</p></div><p id="auth-error" class="form-error hidden" role="alert"></p><button class="btn" type="submit">${signup?'Create free workspace':'Sign in'}${icon('arrow')}</button></form><div class="auth-footer">${signup?'Already have an account?':'New to Confine?'} <button data-action="auth" data-mode="${signup?'login':'register'}">${signup?'Sign in':'Create account'}</button></div>`);
}

async function courseModal(courseId,existing=null) {
  if(!state.user){state.pendingCourse=courseId;authModal();return;}
  const data=await api('/catalog/'+courseId);
  existing ||= state.courses.find(c=>c.course.id===courseId);
  const chosen=new Set(existing?.prerequisites||[]);
  state.modal={type:'course',course:data.course,existing};
  const options=[...data.prerequisites];
  for(const id of chosen)if(!options.some(p=>p.id===id)){try{options.push((await api('/catalog/'+id)).course);}catch{}}
  showModal(existing?'Course boundaries':'Add '+data.course.code,data.course.title,`<div class="form-grid"><div class="quote">${escape(data.course.description)}</div><div><label for="alignment">Source alignment</label><select id="alignment" class="field"><option value="catalog" ${existing?.mode==='catalog'?'selected':''}>Catalog topics + references + uploads</option><option value="strict" ${existing?.mode!=='catalog'?'selected':''}>Class materials + selected prerequisites</option></select><p class="help">Original references are not your instructor’s notes. Class materials mode needs your uploads.</p></div><div><label>Prerequisites you want to permit</label><p class="help">Check the official requirement below, then select the courses you actually took. Alternatives are not automatically approved.</p><div class="quote">${escape(data.course.prerequisite_text||'No prerequisite courses listed. See the official catalog for enrollment restrictions.')}</div><div class="prereq-options" id="prereq-options">${options.filter(p=>p.id!==data.course.id).map(p=>prereqOption(p,chosen.has(p.id))).join('')||'<p class="help">No prerequisite course codes found.</p>'}</div><div class="inline-form spaced"><input class="field" id="extra-prereq" placeholder="Add another prerequisite, e.g. MATH 20C" aria-label="Additional prerequisite code"/><button class="btn secondary small" data-action="find-prereq">Find</button></div></div><div><label for="course-term">Term (optional)</label><input id="course-term" class="field" placeholder="Fall 2026" value="${escape(existing?.term)}" maxlength="80"/></div><div><label for="course-instructor">Instructor (optional)</label><input id="course-instructor" class="field" placeholder="Your instructor’s name" value="${escape(existing?.instructor)}" maxlength="100"/></div><div><label for="course-topic">Section topic (especially for CSE 190)</label><input id="course-topic" class="field" placeholder="The subject taught in your section" value="${escape(existing?.topic)}" maxlength="200"/><p class="help">A topic label sets context. Add course material to support detailed explanations.</p></div><a href="${escape(data.course.source_url)}" target="_blank" rel="noopener noreferrer">View official course catalog ↗</a></div>`,`<button class="btn secondary" data-action="close-modal">Cancel</button><button class="btn" data-action="save-course">${existing?'Save boundaries':'Add to workspace'}${icon('arrow')}</button>`);
}
function prereqOption(course,selected=false) { return `<label class="prereq-item label-inline"><input type="checkbox" value="${course.id}" ${selected?'checked':''}/><span>${escape(course.code)}<small>${escape(course.title)}</small></span></label>`; }

async function uploadMaterial(file) {
  const course=currentCourse();if(!file||!course)return;
  toast('Reading your material…');
  const form=new FormData();form.append('enrollment_id',course.id);form.append('file',file);
  await api('/materials',{method:'POST',body:form});
  await loadWorkspace();await loadCourseData();render();toast('Material added. It is available only in this course workspace.');
}

async function uploadAssignment(file) {
  const course=currentCourse();if(!file||!course)return;
  toast('Extracting assignment questions…');
  const form=new FormData();form.append('enrollment_id',course.id);form.append('file',file);
  const assignment=await api('/assignments/preview',{method:'POST',body:form});
  await loadWorkspace();render();assignmentModal(assignment);
}

function assignmentModal(assignment) {
  state.modal={type:'assignment',assignment};
  const draft=assignment.status==='draft';
  const body=draft?`<div class="notice">${icon('info')}<div>Check the question text, shared instructions, and any scan transcription. You can split or edit questions here before generation.</div></div><div id="question-editors">${assignment.questions.map((q,i)=>questionEditor(q,i)).join('')}</div><button class="text-button spaced" data-action="add-question">${icon('plus')}Add a question</button><p class="file-note">Up to 30 questions. Each supported question uses one response; missing evidence restores that response. Your available balance: ${(state.user?.credits||0)+(state.user?.topup_credits||0)}.</p>${!state.config.ai_ready?'<div class="notice amber spaced">Connect the server’s AI API key to generate solutions. This preview has been saved.</div>':''}`:`${assignment.error?`<div class="notice amber">${escape(assignment.error)}</div>`:''}${['queued','running'].includes(assignment.status)?`<div class="notice"><span class="spinner"></span>${assignment.progress}/${assignment.questions.length} questions processed. You can close this window; work continues.</div>`:''}${assignment.results.map((r,i)=>`<section class="assignment-result"><h3>Question ${i+1}</h3><div class="long-copy">${escape(r.question)}</div>${answerHTML(r.answer)}</section>`).join('')||'<p class="muted">No completed answers yet.</p>'}`;
  const foot=draft?`<button class="btn secondary small" data-action="delete-assignment" data-id="${assignment.id}">Delete draft</button><button class="btn secondary" data-action="save-assignment">Save for later</button><button class="btn" data-action="run-assignment" ${!state.config.ai_ready?'disabled':''}>Generate solution file ${icon('arrow')}</button>`:`<button class="btn secondary small" data-action="delete-assignment" data-id="${assignment.id}" ${['running','queued'].includes(assignment.status)?'disabled':''}>Delete assignment</button>${assignment.download_ready?`<a class="btn secondary" href="/api/assignments/${assignment.id}/download?format=md">Editable text</a><a class="btn" href="/api/assignments/${assignment.id}/download">${icon('download')}Download PDF</a>`:''}<button class="btn secondary" data-action="close-modal">Close</button>`;
  showModal(assignment.title,draft?'Review before generating':`${assignment.status} · ${assignment.questions.length} questions`,body,foot,true);
}
function questionEditor(question,index) { return `<div class="question-editor"><div class="row"><label>Question ${index+1}</label><button class="icon-button" data-action="remove-question" aria-label="Remove question ${index+1}">${icon('trash')}</button></div><textarea class="field" data-question maxlength="12000" aria-label="Question ${index+1}">${escape(question)}</textarea></div>`; }

async function pollAssignments() {
  if(!state.user)return;
  try {
    const previous=state.assignments;
    state.assignments=await api('/assignments');
    const completed=state.assignments.filter(a=>['completed','partial','failed'].includes(a.status)&&previous.some(p=>p.id===a.id&&['queued','running'].includes(p.status)));
    if(completed.length){await refreshUser();toast(completed[0].status==='failed'?'Assignment stopped. Unused responses were restored.':'Your assignment document is ready.');}
    const modal=state.modal;
    if(modal?.type==='assignment'&&['queued','running'].includes(modal.assignment.status)) {
      const fresh=await api('/assignments/'+modal.assignment.id);
      if(fresh.status!==modal.assignment.status||fresh.progress!==modal.assignment.progress)assignmentModal(fresh);
    }
    if(state.page==='assignments'&&JSON.stringify(previous)!==JSON.stringify(state.assignments))render();
  } catch(error) { if(error.status===401){state.user=null;clearInterval(pollTimer);render();} }
}

document.addEventListener('click', async event=>{
  const button=event.target.closest('[data-action]');if(!button||button.disabled)return;
  const action=button.dataset.action;
  if(['auth','close-modal','mobile','mobile-close','citation','suggestion','remove-question','add-question'].includes(action)){
    if(action==='auth')authModal(button.dataset.mode);
    if(action==='close-modal')closeModal();
    if(action==='mobile')$('.sidebar').classList.add('open');
    if(action==='mobile-close')$('.sidebar').classList.remove('open');
    if(action==='citation'){const source=document.getElementById('source-'+button.dataset.id);if(source){source.open=true;source.scrollIntoView({block:'nearest'});}}
    if(action==='suggestion'){$('#question').value=button.dataset.question;$('#question').focus();}
    if(action==='remove-question'){if(document.querySelectorAll('[data-question]').length>1)button.closest('.question-editor').remove();else toast('Keep at least one question.');}
    if(action==='add-question'){const count=document.querySelectorAll('[data-question]').length;if(count<30)$('#question-editors').insertAdjacentHTML('beforeend',questionEditor('',count));else toast('An assignment can contain up to 30 questions.');}
    return;
  }
  button.disabled=true;
  try {
    if(action==='nav')await navigate(button.dataset.page);
    if(action==='add-course')await courseModal(button.dataset.id);
    if(action==='open-course'){state.selected=button.dataset.id;await navigate('tutor');}
    if(action==='course-settings')await courseModal(currentCourse().course.id,currentCourse());
    if(action==='save-course'){
      const {course,existing}=state.modal;
      const payload={course_id:course.id,prerequisites:[...document.querySelectorAll('#prereq-options input:checked')].map(i=>i.value),mode:$('#alignment').value,term:$('#course-term').value,instructor:$('#course-instructor').value,topic:$('#course-topic').value};
      const saved=existing?await api('/courses/'+existing.id,{method:'PUT',body:JSON.stringify(payload)}):await post('/courses',payload);
      state.selected=saved.id;await loadWorkspace();closeModal();await navigate('tutor');toast('Course boundaries saved.');
    }
    if(action==='find-prereq'){
      const query=$('#extra-prereq').value.trim();
      if(!query)throw new Error('Enter a prerequisite course code.');
      const matches=await api('/catalog?school=ucsd&q='+encodeURIComponent(query));
      if(!matches.length)throw new Error('No matching course found in the imported catalog.');
      for(const course of matches.slice(0,6))if(course.id!==state.modal.course.id&&!$('#prereq-options input[value="'+course.id+'"]'))$('#prereq-options').insertAdjacentHTML('beforeend',prereqOption(course));
      $('#extra-prereq').value='';
    }
    if(action==='add-set'){
      if(!state.user){authModal();return;}
      for(const course of state.featured)await post('/courses',{course_id:course.id,prerequisites:[]});
      await loadWorkspace();render();toast('Your five courses are ready. Select a course to add materials and approve prerequisites.');
    }
    if(action==='upload-material')$('#material-file').click();
    if(action==='delete-material'){
      const doc=state.materials.find(d=>d.id===button.dataset.id);
      showModal('Remove this material?',doc?.name||'Course material','<p class="long-copy">This passage will no longer be available to future answers. Previously saved answers retain their cited excerpts.</p>',`<button class="btn secondary" data-action="close-modal">Keep material</button><button class="btn danger" data-action="confirm-delete-material" data-id="${button.dataset.id}">Remove material</button>`);
    }
    if(action==='confirm-delete-material'){await api('/materials/'+button.dataset.id,{method:'DELETE'});closeModal();await loadWorkspace();await loadCourseData();render();toast('Material removed.');}
    if(action==='view-assignment')assignmentModal(await api('/assignments/'+button.dataset.id));
    if(action==='save-assignment'){
      const questions=[...document.querySelectorAll('[data-question]')].map(t=>t.value.trim());
      if(questions.some(q=>!q))throw new Error('Fill in each question or remove empty questions.');
      await api('/assignments/'+state.modal.assignment.id,{method:'PUT',body:JSON.stringify({questions})});
      closeModal();await loadWorkspace();render();toast('Assignment draft saved.');
    }
    if(action==='run-assignment'){
      const questions=[...document.querySelectorAll('[data-question]')].map(t=>t.value.trim());
      if(questions.some(q=>!q))throw new Error('Fill in each question or remove empty questions.');
      const result=await post('/assignments/'+state.modal.assignment.id+'/run',{questions,request_key:key()});
      await loadWorkspace();await refreshUser();render();assignmentModal(result);toast('Assignment queued. You can leave this window open or come back later.');
    }
    if(action==='delete-assignment'){
      showModal('Delete this assignment?','This also removes its saved solution file.','<p class="long-copy">Deleting a document does not restore responses already used to generate supported answers.</p>',`<button class="btn secondary" data-action="close-modal">Keep it</button><button class="btn danger" data-action="confirm-delete-assignment" data-id="${button.dataset.id}">Delete assignment</button>`);
    }
    if(action==='confirm-delete-assignment'){await api('/assignments/'+button.dataset.id,{method:'DELETE'});closeModal();await loadWorkspace();render();toast('Assignment deleted.');}
    if(action==='logout'){await post('/auth/logout',{});state.user=null;state.courses=[];state.assignments=[];state.selected=null;state.page='home';render();}
    if(action==='checkout'||action==='checkout-topup')location.assign((await post('/billing/checkout'+(action==='checkout-topup'?'?topup=true':''),{})).url);
    if(action==='billing-portal')location.assign((await post('/billing/portal',{})).url);
    if(action==='delete-account')showModal('Delete your account?','Your courses, materials, chats, and assignment documents will be removed.','<p class="long-copy">This cannot be undone. Download any documents you want to keep before proceeding.</p>',`<button class="btn secondary" data-action="close-modal">Keep account</button><button class="btn danger" data-action="confirm-delete-account">Delete account</button>`);
    if(action==='confirm-delete-account'){await api('/account',{method:'DELETE'});closeModal();state.user=null;state.courses=[];state.assignments=[];state.selected=null;state.page='home';render();toast('Your account and saved materials were deleted.');}
  } catch(error) { toast(error.message); }
  finally { button.disabled=false; }
});

document.addEventListener('submit',async event=>{
  if(!['auth-form','chat-form'].includes(event.target.id))return;
  event.preventDefault();
  const submit=$('button[type="submit"]',event.target);submit.disabled=true;
  try {
    if(event.target.id==='auth-form'){
      const mode=state.modal.mode;
      state.user=await post('/auth/'+mode,{email:$('#auth-email').value,password:$('#auth-password').value,name:$('#auth-name')?.value||''});
      closeModal();await loadWorkspace();render();
      if(state.pendingCourse){const id=state.pendingCourse;state.pendingCourse=null;await courseModal(id);}
      toast(mode==='register'?'Your free workspace is ready.':'Welcome back.');
    } else {
      const question=$('#question').value.trim();if(!question)return;
      state.busy=true;render();
      const result=await post('/chat',{enrollment_id:currentCourse().id,question,request_key:key()});
      state.chats.push(result);await refreshUser();
    }
  } catch(error) {
    if(event.target.id==='auth-form') { const el=$('#auth-error');if(el){el.textContent=error.message;el.classList.remove('hidden');} }
    else toast(error.message);
  } finally {submit.disabled=false;if(event.target.id==='chat-form'){state.busy=false;render();$('#question')?.focus();}}
});

document.addEventListener('input',event=>{
  if(event.target.id==='catalog-search'){state.search=event.target.value;clearTimeout(searchTimer);searchTimer=setTimeout(loadCatalog,250);}
});
document.addEventListener('change',async event=>{
  try {
    if(event.target.id==='school-select'){state.school=event.target.value;await loadCatalog();}
    if(event.target.id==='active-course'){state.selected=event.target.value;await loadCourseData();render();}
    if(event.target.id==='material-file')await uploadMaterial(event.target.files[0]);
    if(event.target.id==='assignment-file')await uploadAssignment(event.target.files[0]);
  } catch(error){toast(error.message);event.target.value='';}
});
document.addEventListener('keydown',event=>{
  if(event.target.id==='question'&&event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(state.config.ai_ready&&!state.busy)$('#chat-form').requestSubmit();}
  if(!$('.modal'))return;
  if(event.key==='Escape'){closeModal();return;}
  if(event.key==='Tab'){
    const items=[...document.querySelectorAll('.modal button:not(:disabled),.modal input,.modal select,.modal textarea,.modal a[href]')].filter(el=>!el.closest('.hidden'));
    const first=items[0],last=items.at(-1);
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
  }
});
for(const type of ['dragover','dragleave','drop'])document.addEventListener(type,event=>{
  const zone=event.target.closest('#assignment-dropzone');if(!zone)return;
  event.preventDefault();zone.classList.toggle('dragging',type==='dragover');
  if(type==='drop')uploadAssignment(event.dataTransfer.files[0]).catch(error=>toast(error.message));
});

async function start() {
  try {
    state.config=await api('/config');
    state.featured=(await Promise.all(state.config.featured_courses.map(id=>api('/catalog/'+id).catch(()=>null)))).filter(Boolean).map(d=>d.course);
    try{state.user=await api('/auth/me');}catch(error){if(error.status!==401)throw error;}
    await loadWorkspace();render();
    pollTimer=setInterval(pollAssignments,4000);
    if(new URLSearchParams(location.search).has('payment')){toast('Checking your payment status. Your allowance updates after payment confirmation.');history.replaceState(null,'','/');}
  } catch(error){$('#app').innerHTML=`<div class="loading-screen"><h1>We couldn’t open the workspace.</h1><p>${escape(error.message)}</p><a href="/" class="btn">Try again</a></div>`;}
}
start();
