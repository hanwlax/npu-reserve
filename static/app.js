(() => {
  const root = document.getElementById('npu-reserve');
  const workspace = root.querySelector('#npu-workspace');
  const identity = root.querySelector('#npu-identity');
  const lockDialog = root.querySelector('#npu-lock-dialog');
  let machines = {};
  let groups = [];
  let accounts = [];
  let administrators = [];
  let records = [];
  let upcoming = [];
  let pastEnd = 0;
  let cutoff = 0;
  let windowStart = 0;
  let revision = 0;
  const activeMachines = () => Object.keys(machines).map(Number);
  let view = 'login';
  let user = null;
  let section = 'access';
  let lockMode = 'lock';
  let accountIndex = 0;
  let groupIndex = 0;
  let draft = null;
  let drag = null;
  let notice = null;
  let releaseEditor = null;
  let pendingEdit = null;
  let pendingConfirmation = null;
  let busy = false;
  let polling = false;

  const escape = value => String(value).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
  const time = slot => {
    const mins = ((480 + 30 * slot) % 1440 + 1440) % 1440;
    return String(Math.floor(mins / 60)).padStart(2,'0') + ':' + String(mins % 60).padStart(2,'0');
  };
  const localDate = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit'});
  const localWeekday = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',weekday:'short'});
  const dateAt = slot => new Date((windowStart + slot*1800)*1000);
  const stamp = slot => localDate.format(dateAt(slot))+' '+time(slot);
  const dayLabel = slot => localDate.format(dateAt(slot))+' '+localWeekday.format(dateAt(slot));
  const windowLabel = () => range({s:0,e:48})+' · 北京时间';
  const boardNote = () => '单行点击或拖选'+
    (view==='admin'?' · 可跨已预约和已锁定时段':' · 点击自己的预约可释放，也可在其中拖选一段')+
    ' · 30 分钟一格';
  const range = ({s,e}) => stamp(s) + ' – ' + stamp(e);
  const allowed = (account, machineGroups = groups) => [...new Set([
    ...account.direct,
    ...machineGroups.filter(group => account.groups.includes(group.id)).flatMap(group => group.members)
  ])].filter(i => machines[i]).sort((a,b) => a-b);
  const ownedBookings = account => upcoming.filter(r => r.kind==='booked' && r.owner===account.username);
  function ownRange(candidate) {
    if (view !== 'user' || candidate.s < pastEnd) return null;
    return ownedBookings(user).find(r => r.m === candidate.m && r.s <= candidate.s && candidate.e <= r.e);
  }

  const affectedBookings = (account, nextMachines) => {
    const before = allowed(account);
    return upcoming.filter(r => r.kind==='booked' && r.owner===account.username && before.includes(r.m) && !nextMachines.includes(r.m));
  };
  const releasedRange = record => range({s:Math.max(record.s,cutoff),e:record.e});

  const overlaps = (record, {m,s,e}) => record.m === m && record.s < e && s < record.e;
  const hit = candidate => records.find(r => overlaps(r,candidate));
  const valid = candidate => candidate && candidate.s >= pastEnd && candidate.s < candidate.e &&
    candidate.e <= 48 && (view === 'admin' || !hit(candidate));
  const overlappingBookings = candidate => records.filter(r => r.kind === 'booked' && overlaps(r,candidate));
  const overlappingLocks = candidate => records.filter(r => r.kind === 'locked' && overlaps(r,candidate));
  const unlocking = () => view === 'admin' && lockMode === 'unlock';
  const intersection = (record, candidate) => ({s:Math.max(record.s,candidate.s),e:Math.min(record.e,candidate.e)});

  function lockGaps({m,s,e}) {
    const covered = overlappingLocks({m,s,e}).sort((a,b) => a.s-b.s);
    const gaps = [];
    let cursor = s;
    for (const lock of covered) {
      if (lock.s > cursor) gaps.push({m,s:cursor,e:lock.s});
      cursor = Math.max(cursor,lock.e);
    }
    if (cursor < e) gaps.push({m,s:cursor,e});
    return gaps;
  }
  const status = record => record.kind === 'locked' ? '管理员锁定' :
    view === 'user' && record.owner === user.username ? '我的预约' : '已预约';
  const say = (message, error = false) => { notice = {message,error}; };
  const noticeHTML = () => notice ? '<div class="notice' + (notice.error ? ' error" role="alert"' : '" role="status"') + '>' + escape(notice.message) + '</div>' : '';
  const button = (action,label,style='') => '<button type="button" class="button '+style+'" data-action="'+action+'">'+label+'</button>';

  function chrome() {
    identity.innerHTML = view === 'login' ? '' : '<span class="pill small">'+escape(user.name)+(view==='admin'?' · 管理员':'')+'</span>' +
      '<button class="link-button small" type="button" data-action="change-password">修改密码</button>'+
      '<button class="link-button small" type="button" data-action="logout">退出</button>';
  }

  function intervalDetail(record) {
    return status(record)+' · '+range(record)+' · 操作人 '+(record.operator || '未记录')+
      (record.reason ? ' · '+record.reason : '');
  }

  function block(record, kind, label, detail) {
    const tooltip = detail ? ' title="'+escape(detail)+'"' : '';
    return '<div class="block '+kind+'" style="left:'+record.s/48*100+'%;width:'+(record.e-record.s)/48*100+'%"'+tooltip+' aria-hidden="true">' +
      (record.e-record.s >= 4 ? label : '') + '</div>';
  }

  function draftBlock(candidate) {
    return block(candidate,view==='admin'?'draft admin-draft':'draft','待提交');
  }

  function track(m) {
    const {name,ip,spec} = machines[m];
    const releaseRange = releaseEditor?.record.m === m ? releaseCandidate() : null;
    const row = records.filter(r => r.m === m);
    const detail = row.map(intervalDetail).join('，');
    return '<div class="board-row"><div class="machine"><button type="button" data-machine="'+m+'" data-action="select-machine" aria-label="选择 '+escape(name)+' 的时段">'+
      escape(name)+'</button><span class="quiet small">'+escape(ip)+'</span>'+
      (spec?'<span class="quiet small">'+escape(spec)+'</span>':'')+'</div><div class="track" data-track="'+m+'" role="group" aria-label="'+escape(detail || '无占用时段')+'">'+
      block({s:0,e:pastEnd},'past','')+row.filter(r => r.e > pastEnd).map(r =>
        block({...r,s:Math.max(r.s,pastEnd)},status(r)==='我的预约' ? 'mine' : r.kind,status(r),intervalDetail(r))).join('')+
      (draft?.m === m && valid(draft) ? draftBlock(draft) : '')+
      (releaseRange ? block(releaseRange,'release-draft','') : '')+'</div></div>';
  }

  function timeline() {
    const ids = activeMachines();
    if (!ids.length) return '<div class="panel"><h2>暂无可预约机器</h2><p class="quiet">'+
      (view==='admin'?'请先在机器资源池中新增机器。':'请私聊管理员申请机器权限。')+'</p></div>';
    const ticks = [0,4,8,12,16,20,24,28,32,36,40,44,48].map(i => {
      const cls = i===0?' first':i===48?' last':i===32?' midnight':'';
      return '<span class="tick'+cls+'" '+(i===48?'':'style="left:'+i/48*100+'%"')+'>'+time(i)+'</span>';
    }).join('');
    const ungrouped = ids.filter(i => !groups.some(g => g.members.includes(i)));
    const entries = [...groups,{name:'未分组',members:ungrouped}];
    const rows = entries.map(g => {
      const list = g.members.filter(i => ids.includes(i));
      return list.length ? '<div class="group">'+escape(g.name)+' <span aria-hidden="true">·</span> '+list.length+' 台</div>'+list.map(track).join('') : '';
    }).join('');
    return '<div class="board" aria-label="机器预约时间轴"><div class="board-grid"><div class="board-row"><div class="machine"><strong>机器</strong><span class="quiet small">IP 地址 · 配置</span></div>'+
      '<div class="axis"><span class="day">今天 · '+dayLabel(0)+'</span><span class="day next">次日 · '+dayLabel(48)+'</span>'+ticks+'</div></div>'+rows+'</div></div>';
  }

  function options(selected, min, max) {
    return Array.from({length:Math.max(0,max-min+1)},(_,i) => i+min).map(i =>
      '<option value="'+i+'"'+(i===selected?' selected':'')+'>'+stamp(i)+'</option>').join('');
  }

  function draftDescription() {
    if (!valid(draft)) return view==='admin' ? '请选择有效的起止时间。' : '请选择连续的空闲时段。';
    const length = range(draft)+' · '+((draft.e-draft.s)/2)+' 小时';
    if (view !== 'admin') return length;
    if (unlocking()) {
      const count = overlappingLocks(draft).length;
      return length+(count?' · 涉及 '+count+' 段锁定':' · 选区内没有锁定');
    }
    const affected = overlappingBookings(draft).length;
    if (affected) return length+' · 涉及 '+affected+' 笔预约';
    return length+(lockGaps(draft).length ? '' : ' · 已全部锁定');
  }

  function myBookings() {
    const current = ownedBookings(user).sort((a,b) => a.s-b.s);
    return '<section class="panel" aria-label="我的预约"><h2>我的预约</h2>' +
      (current.length ? '<div class="listing">'+current.map(r => '<div class="list-row"><div><strong>'+escape(machines[r.m].name)+
        '</strong> <span class="pill small">'+(r.s <= cutoff?'使用中':'待开始')+'</span><div class="quiet small">'+range(r)+
        '</div></div><button type="button" class="button" data-release="'+r.id+'" aria-label="释放 '+escape(machines[r.m].name)+
        ' 的预约 '+range(r)+'">释放机时</button></div>').join('')+'</div>' :
        '<p class="quiet">当前没有待开始或使用中的预约。</p>')+'</section>';
  }

  function beginRelease(record, partial = null) {
    if (view !== 'user' || !ownedBookings(user).includes(record) || !machines[record.m]) return;
    reset();
    const start = Math.max(record.s,cutoff);
    releaseEditor = {record,mode:partial?'partial':'whole',s:partial?.s ?? start,e:partial?.e ?? record.e};
    patch();
    root.querySelector('#npu-release [name="mode"]:checked').focus();
  }

  function releaseCandidate() {
    const {record,mode,s,e} = releaseEditor;
    const first = Math.max(record.s,cutoff);
    if (mode === 'whole') return {m:record.m,s:first,e:record.e};
    if (s >= first && s < e && e <= record.e) return {m:record.m,s,e};
    return null;
  }

  function releasePreview() {
    const candidate = releaseCandidate();
    if (!candidate) return '请选择预约范围内的起止时间，结束须晚于开始。';
    const record = releaseEditor.record;
    const remaining = remainingRanges({...record,s:Math.max(record.s,cutoff)},candidate);
    return '将释放 '+range(candidate)+'。'+
      (remaining.length?'保留 '+remaining.map(range).join('、')+'。':'')+
      (record.s < cutoff?'已使用的时段保留。':'')+'释放后，该时段可再次预约。';
  }

  function releaseForm() {
    const {record,mode,s,e} = releaseEditor;
    const first = Math.max(record.s,cutoff);
    const wholeLabel = record.s < cutoff ? '释放全部剩余机时' : '释放整段预约';
    const radio = (value,label) => '<label><input type="radio" name="mode" value="'+value+'" '+(mode===value?'checked':'')+'> '+label+'</label>';
    return '<form class="panel" id="npu-release" aria-label="确认释放"><h2>释放预约 · '+escape(machines[record.m].name)+'</h2>'+
      '<p class="quiet small">原预约 · '+range(record)+'</p>'+
      '<fieldset class="checks release-modes"><legend>释放范围</legend>'+radio('whole',wholeLabel)+radio('partial','仅释放一段')+'</fieldset>'+
      '<div class="release-fields" '+(mode==='partial'?'':'hidden')+'>'+
      '<label class="field">释放开始<select name="start">'+options(s,first,record.e-1)+'</select></label>'+
      '<label class="field">释放结束<select name="end">'+options(e,first+1,record.e)+'</select></label></div>'+
      '<p class="quiet" id="npu-release-preview" aria-live="polite">'+releasePreview()+'</p>'+
      '<div class="controls"><button type="submit" class="button danger" '+(releaseCandidate()?'':'disabled')+'>确认释放</button>'+
      button('cancel-release','取消')+'</div></form>';
  }

  function readRelease(data) {
    releaseEditor.mode = data.get('mode') === 'partial' ? 'partial' : 'whole';
    releaseEditor.s = Number(data.get('start'));
    releaseEditor.e = Number(data.get('end'));
  }

  function selection() {
    if (releaseEditor) return releaseForm();
    const admin = view === 'admin';
    const action = unlocking() ? '解锁' : '锁定';
    if (!draft) return '<div class="panel"><h2>'+ (admin ? '选择'+action+'时段' : '选择预约时段') +'</h2><p class="quiet">'+
      (unlocking()?'选中要解除锁定的范围。只解除其中的锁定。':
        admin?'可跨过已预约和已锁定的时段拖选，也可点击机器名后调整起止时间。':'在空闲时段点击或拖选，也可点击机器名后调整起止时间。')+'</p></div>';
    const needsReason = admin && !unlocking() && valid(draft) && lockGaps(draft).length > 0;
    return '<div class="panel"><div class="summary"><div><h2>'+(admin?action+'时段':'预约确认')+' · '+escape(machines[draft.m].name)+
      '</h2><div class="quiet small" id="npu-draft-time" aria-live="polite">'+draftDescription()+'</div></div>'+
      '</div><form id="npu-reservation" class="controls"><label class="field">开始<select name="start">'+options(draft.s,pastEnd,47)+'</select></label>'+
      '<label class="field">结束<select name="end">'+options(draft.e,pastEnd+1,48)+'</select></label>'+
      (admin?'<label class="field" '+(needsReason?'':'hidden')+'>锁定原因<input name="reason" placeholder="例如例行维护" maxlength="40" '+(needsReason?'required':'disabled')+'></label>':'')+
      '<button class="button primary" type="submit">'+(admin?'确认'+action:'提交预约')+'</button>'+button('clear','清除选择')+'</form></div>';
  }

  function schedule() {
    const admin = view === 'admin';
    const labels = [['','可预约'],['booked','已预约'],['locked','管理员锁定'],...(admin?[]:[['mine','我的预约']]),['past','已过去'],['draft','待提交']];
    return '<div class="intro"><div><div class="eyebrow">'+(admin?'资源管理':'预约工作台')+'</div><h1>'+(admin?'时段锁定与解锁':'机器预约')+
      '</h1><p class="quiet" id="npu-window">'+windowLabel()+'</p></div><div class="legend" aria-label="时段图例">'+
      labels.map(([key,label])=>'<span><b class="swatch '+key+'"></b>'+label+'</span>').join('')+'</div></div>'+
      (admin ? adminNav()+
        '<div class="nav" role="group" aria-label="机时操作">'+[['lock','锁定'],['unlock','解锁']].map(([key,label]) =>
          '<button type="button" data-mode="'+key+'" aria-pressed="'+(lockMode===key)+'">'+label+'</button>').join('')+'</div>' : '')+
      '<section aria-label="预约时间轴"><div id="npu-board-slot">'+timeline()+'</div>'+
      '<p class="board-note">'+boardNote()+'</p></section>'+
      '<div id="npu-notice" aria-live="polite">'+noticeHTML()+'</div><div id="npu-selection">'+(activeMachines().length ? selection() : '')+'</div>'+
      (admin?'':'<div id="npu-my-bookings">'+myBookings()+'</div>');
  }

  function adminNav() {
    return '<nav class="nav" aria-label="管理菜单">'+[['access','账户与授权'],['pool','机器资源池'],['groups','机器分组'],['locks','时段锁定']].map(([key,label]) =>
      '<button type="button" data-section="'+key+'" aria-pressed="'+(section===key)+'">'+label+'</button>').join('')+'</nav>';
  }

  function resetPasswordButton(account) {
    return '<button type="button" class="button" data-reset-account="'+account.id+'">重置密码</button>';
  }

  function access() {
    const account = accounts[accountIndex];
    return '<div class="intro"><div><div class="eyebrow">资源管理</div><h1>账户与授权</h1><p class="quiet">用户私聊申请后，由管理员建号并分配机器权限。</p></div>'+
      button('new-account','＋ 创建账户','primary')+'</div>'+adminNav()+'<div aria-live="polite">'+noticeHTML()+'</div><div class="admin-grid">'+
      '<section class="panel"><h2>用户账户</h2><div class="listing">'+accounts.map((a,i)=>'<div class="list-row"><div><strong>'+escape(a.name)+'</strong>'+
      '<div class="quiet small">'+escape(a.username)+' · '+allowed(a).length+' 台机器</div></div>'+
      '<div class="controls"><button type="button" class="button" data-account="'+i+'" '+(i===accountIndex?'aria-current="true"':'')+'>'+(i===accountIndex?'正在编辑':'编辑权限')+'</button>'+
      '<button type="button" class="button" data-view-password="'+a.id+'">查看密码</button>'+
      resetPasswordButton(a)+'</div></div>').join('')+'</div><h2>管理员账户</h2><div class="listing">'+administrators.map(a=>
        '<div class="list-row"><div><strong>'+escape(a.name)+'</strong><div class="quiet small">'+escape(a.username)+'</div></div>'+
        resetPasswordButton(a)+'</div>').join('')+'</div></section>'+
      (account ? '<form class="panel" id="npu-access"><h2>'+escape(account.name)+' · 机器权限</h2><fieldset class="checks"><legend>按机器组分配</legend>'+
      groups.map(g=>check('group',g.id,g.name+' · '+g.members.length+' 台',account.groups.includes(g.id))).join('')+'</fieldset>'+
      '<fieldset class="checks"><legend>单独分配</legend>'+activeMachines().map(i=>check('direct',i,machineLabel(i),account.direct.includes(i))).join('')+'</fieldset>'+
      '<p class="quiet small" id="npu-effective" aria-live="polite">'+escape(effective(account))+'</p>'+
      '<div data-review aria-live="polite">'+saveButton('保存权限')+'</div></form>' : '<section class="panel"><h2>暂无用户账户</h2><p class="quiet">创建账户后即可分配机器权限。</p></section>')+'</div>';
  }

  function check(name,value,label,checked) {
    return '<label><input type="checkbox" name="'+name+'" value="'+value+'" '+(checked?'checked':'')+'> '+escape(label)+'</label>';
  }

  function effective(account, next = account) {
    const ids = allowed(next);
    const names = ids.map(i => machines[i].name);
    const lost = allowed(account).filter(i => !ids.includes(i)).map(i => machines[i].name);
    const impact = affectedBookings(account,ids).length;
    return '用户可见 · '+(names.length ? names.join('、') : '暂无机器')+
      (lost.length ? '。保存后将收回 '+lost.join('、')+' 的访问权限' : '')+
      (impact ? '，并释放 '+impact+' 笔预约的剩余时段' : '')+'。';
  }

  function readAccess(data) {
    return {groups:data.getAll('group').map(Number),direct:data.getAll('direct').map(Number)};
  }

  function saveButton(label) {
    return '<button type="submit" class="button primary">'+label+'</button>';
  }

  function clearReview() {
    if (!pendingEdit) return;
    const {box,label} = pendingEdit;
    pendingEdit = null;
    box.innerHTML = saveButton(label);
  }

  function reviewEdit(form, label, impact, commit) {
    if (pendingEdit) return;
    if (!impact.length) {
      commit();
      return;
    }
    const box = form.querySelector('[data-review]');
    pendingEdit = {box,label,commit};
    box.innerHTML = '<div class="review"><strong>保存后将自动释放 '+impact.length+' 笔预约</strong>'+
      '<p>以下用户将失去对应机器的访问权限，列出的时段将重新开放预约。</p><ul>'+
      impact.map(({account,record}) => '<li>'+escape(account.name)+' · '+escape(machines[record.m].name)+' · '+releasedRange(record)+'</li>').join('')+
      '</ul><div class="controls">'+button('confirm-edit','确认保存并释放','danger')+button('cancel-edit','返回编辑')+'</div></div>';
    box.querySelector('[data-action="cancel-edit"]').focus();
  }

  function groupPage() {
    const group = groups[groupIndex];
    return '<div class="intro"><div><div class="eyebrow">资源管理</div><h1>机器分组</h1><p class="quiet">把机器加入组后，可将整组分配给用户。</p></div>'+
      button('new-group','＋ 新建分组','primary')+'</div>'+adminNav()+'<div aria-live="polite">'+noticeHTML()+'</div><div class="admin-grid">'+
      '<section class="panel"><h2>机器组</h2><div class="listing">'+groups.map((g,i)=>'<div class="list-row"><div><strong>'+escape(g.name)+'</strong>'+
      '<div class="quiet small">'+g.members.length+' 台机器 · '+accounts.filter(a=>a.groups.includes(g.id)).length+' 个账户获授权</div></div>'+
      '<button type="button" class="button" data-group="'+i+'">'+(i===groupIndex?'正在编辑':'编辑分组')+'</button></div>').join('')+'</div></section>'+
      (group ? '<form class="panel" id="npu-group"><h2>编辑机器组</h2><label class="field">组名<input name="name" value="'+escape(group.name)+'" required maxlength="24"></label>'+
      '<fieldset class="checks"><legend>组内机器</legend>'+activeMachines().map(i=>{
        const other = groups.find(g=>g.id!==group.id && g.members.includes(i));
        return check('member',i,machineLabel(i)+(other?'（将从 '+other.name+' 移入）':''),group.members.includes(i));
      }).join('')+'</fieldset><p class="quiet small">每台机器归入一个组。保存后，相关组的用户权限随成员更新。</p>'+
      '<div data-review aria-live="polite">'+saveButton('保存分组')+'</div></form>' : '<section class="panel"><h2>暂无机器组</h2><p class="quiet">新建分组后可添加成员。</p></section>')+'</div>';
  }

  function machineLabel(i) {
    const {name,ip} = machines[i];
    return name+' · '+ip;
  }

  function resourcePool() {
    const rows = activeMachines().map(i => {
      const {name,ip,spec} = machines[i];
      const group = groups.find(g => g.members.includes(i));
      return '<div class="list-row"><div><strong>'+escape(name)+'</strong>'+
        '<div class="quiet small">'+escape(ip)+' · '+escape(group?.name || '未分组')+'</div>'+
        (spec?'<div class="quiet small">'+escape(spec)+'</div>':'')+'</div>'+
        '<button type="button" class="button" data-remove-machine="'+i+'" aria-label="删除 '+escape(machineLabel(i))+'">删除</button></div>';
    }).join('');
    return '<div class="intro"><div><div class="eyebrow">资源管理</div><h1>机器资源池</h1>'+
      '<p class="quiet">新增机器后，可在机器分组或账户授权中分配。</p></div></div>'+adminNav()+
      '<div aria-live="polite">'+noticeHTML()+'</div><div class="admin-grid"><section class="panel"><h2>机器 · '+activeMachines().length+
      ' 台</h2><div class="listing">'+(rows || '<p class="quiet">资源池暂无机器。</p>')+'</div></section>'+
      '<form class="panel" id="npu-add-machine"><h2>新增机器</h2><div class="create-notice" aria-live="polite"></div>'+
      '<label class="field">机器名称（必填）<input name="name" required maxlength="48" placeholder="例如 a5-05"></label>'+
      '<label class="field">IP 地址（必填）<input name="ip" required maxlength="45" placeholder="IPv4 或 IPv6" autocomplete="off"></label>'+
      '<label class="field">配置（选填）<input name="spec" maxlength="80" placeholder="例如 NPU 型号、卡数或内存"></label>'+
      '<div>'+saveButton('新增机器')+'</div></form></div>';
  }

  function reviewMachineRemoval(m) {
    const affected = upcoming.filter(r => r.m === m);
    const payload = {m,revision};
    const bookings = affected.filter(r => r.kind === 'booked').length;
    showConfirmation('删除机器？',machineLabel(m),
      '<p>删除后，机器将从资源池、分组、账户授权和预约界面移除。</p>'+
      (affected.length?'<div class="review"><p>将释放 '+bookings+' 笔预约的剩余机时，并移除 '+(affected.length-bookings)+' 段锁定的剩余时段。</p></div>':''),
      '确认删除',() => save('remove-machine',payload));
  }

  function login() {
    return '<form class="panel login" id="npu-login"><h1>登录</h1><p class="quiet">没有账户时，请私聊管理员申请账户与机器权限。</p>'+
      '<div data-login-notice aria-live="polite">'+noticeHTML()+'</div><label class="field">账户<input name="username" required maxlength="24" autocomplete="username"></label>'+
      '<label class="field">密码<input name="password" type="password" required maxlength="128" autocomplete="current-password"></label>'+
      '<button class="button primary" type="submit">登录</button></form>';
  }

  function render() {
    pendingEdit = null;
    chrome();
    workspace.innerHTML = view==='login' ? login() : view==='user' || section==='locks' ? schedule() : section==='access' ? access() : section==='pool' ? resourcePool() : groupPage();
  }

  function patch({board = true, form = true} = {}) {
    if (board) {
      const slot = root.querySelector('#npu-board-slot');
      const scroll = slot.querySelector('.board')?.scrollLeft || 0;
      root.querySelector('#npu-window').textContent = windowLabel();
      slot.innerHTML = timeline();
      const updated = slot.querySelector('.board');
      if (updated) updated.scrollLeft = scroll;
    }
    if (form) root.querySelector('#npu-selection').innerHTML = activeMachines().length ? selection() : '';
    const bookings = root.querySelector('#npu-my-bookings');
    if (board && bookings) bookings.innerHTML = myBookings();
    root.querySelector('#npu-notice').innerHTML = noticeHTML();
  }

  function select(candidate) {
    if (valid(candidate)) {
      releaseEditor = null;
      draft = candidate;
      notice = null;
      patch();
      return;
    }
    const occupied = view==='user' && hit(candidate);
    say(occupied ? '所选范围包含'+status(occupied)+'时段（'+range(occupied)+(occupied.reason?' · '+occupied.reason:'')+'）。请选择连续的空闲时段。' :
      '请选择尚未过去且结束时间晚于开始时间的时段。',true);
    patch({board:false,form:false});
  }

  function reset() {
    draft = null;
    notice = null;
    releaseEditor = null;
  }

  function passwordFields(label, canGenerate) {
    return (canGenerate?'<label><input type="checkbox" name="generate" checked> 自动生成密码</label>':'')+
      '<label class="field" '+(canGenerate?'hidden':'')+'>'+label+
      '<input name="password" type="password" minlength="8" maxlength="128" autocomplete="new-password" '+
      (canGenerate?'disabled':'required')+'></label>';
  }

  function passwordPayload(data) {
    return data.has('generate') ? {generate:true} : {password:data.get('password')};
  }

  function showPasswordForm(target) {
    const resetting = Boolean(target);
    const title = resetting ? '重置密码' : '修改密码';
    lockDialog.innerHTML = '<form class="dialog-content" id="npu-password" data-target="'+(target?.id || '')+'">'+
      '<h2 id="npu-lock-title">'+title+'</h2><p id="npu-lock-summary">'+escape((target || user).username)+
      ' · '+(target && target.id!==user.id?'保存后，该账户的会话会退出。':'保存后，其他会话会退出。')+'</p>'+
      '<div class="create-notice" aria-live="polite"></div>'+passwordFields('新密码',resetting)+
      '<div class="controls">'+button('cancel-lock','取消')+saveButton(title)+'</div></form>';
    lockDialog.showModal();
  }

  function showPasswordValue(title, summary, password) {
    lockDialog.innerHTML = '<div class="dialog-content"><h2 id="npu-lock-title">'+escape(title)+'</h2>'+
      '<p id="npu-lock-summary">'+escape(summary)+'</p>'+
      '<label class="field">密码<input id="npu-visible-password" readonly value="'+escape(password)+'" autocomplete="off"></label>'+
      '<div class="controls">'+button('cancel-lock','完成','primary')+'</div></div>';
    lockDialog.showModal();
    const passwordField = lockDialog.querySelector('#npu-visible-password');
    passwordField.focus();
    passwordField.select();
  }

  function showCreate(account) {
    clearReview();
    notice = null;
    const label = account ? '创建账户' : '新建机器组';
    const fields = account ?
      '<label class="field">登录账户<input name="username" required pattern="[a-zA-Z0-9_\\x2d]+" maxlength="24"></label>'+
      '<label class="field">显示名（选填）<input name="name" maxlength="24" placeholder="同登录账户"></label>'+
      passwordFields('初始密码',true)+'<p class="quiet small">建号后私聊告知用户登录信息。</p>' :
      '<label class="field">组名<input name="name" required maxlength="24"></label>';
    workspace.querySelector('.admin-grid').lastElementChild.outerHTML =
      '<form class="panel" id="npu-create" data-kind="'+(account?'account':'group')+'"><h2>'+label+'</h2><div aria-live="polite" class="create-notice"></div>'+fields+
      '<div class="controls"><button class="button primary" type="submit">'+label+'</button>'+button('cancel-create','取消')+'</div></form>';
    workspace.querySelector('#npu-create input').focus();
  }

  root.addEventListener('click', event => {
    const control = event.target.closest('button');
    if (!control || busy) return;
    const data = control.dataset;
    if (data.section) {
      if (section === data.section) return;
      section = data.section;
      reset();
      render();
      return;
    }
    if (data.mode) {
      if (lockMode !== data.mode) {
        lockMode = data.mode;
        reset();
        render();
        root.querySelector('[data-mode="'+lockMode+'"]').focus();
      }
      return;
    }
    if (data.removeMachine !== undefined) {
      reviewMachineRemoval(Number(data.removeMachine));
      return;
    }
    if (data.account !== undefined || data.group !== undefined) {
      if (data.account !== undefined) accountIndex = Number(data.account);
      else groupIndex = Number(data.group);
      notice = null;
      render();
      return;
    }
    if (data.viewPassword !== undefined && view==='admin') {
      const target = accounts.find(account => account.id===Number(data.viewPassword));
      if (target) run(async () => {
        const {password} = await request('view-password',{id:target.id});
        showPasswordValue('查看密码',target.name+' · '+target.username,password);
      });
      return;
    }
    if (data.resetAccount !== undefined && view==='admin') {
      const target = [...accounts,...administrators].find(account => account.id===Number(data.resetAccount));
      if (target) showPasswordForm(target);
      return;
    }
    if (data.release !== undefined) {
      beginRelease(ownedBookings(user).find(r=>r.id===Number(data.release)));
      return;
    }
    switch (data.action) {
      case 'cancel-release': reset(); patch(); break;
      case 'cancel-edit': clearReview(); break;
      case 'confirm-edit': pendingEdit?.commit(); break;
      case 'cancel-lock': closeLockDialog(); break;
      case 'change-password': showPasswordForm(); break;
      case 'confirm-lock': {
        if (!pendingConfirmation) return;
        const commit = pendingConfirmation;
        closeLockDialog();
        commit();
        break;
      }
      case 'logout': run(async () => { await request('logout',{}); signedOut(); }); break;
      case 'clear': reset(); patch(); break;
      case 'new-account': showCreate(true); break;
      case 'new-group': showCreate(false); break;
      case 'cancel-create': render(); break;
      case 'select-machine': {
        const m = Number(data.machine);
        const s = view==='admin' ? pastEnd :
          Array.from({length:48-pastEnd},(_,i)=>pastEnd+i).find(i=>!hit({m,s:i,e:i+1}));
        if (s === undefined) {
          say(machines[m].name+' 在当前窗口内没有空闲时段。',true);
          patch({board:false,form:false});
        } else {
          select({m,s,e:s+1});
          root.querySelector('#npu-reservation select').focus();
        }
        break;
      }
    }
  });

  root.addEventListener('change', event => {
    const form = event.target.form;
    if (event.target.name==='generate' && (form?.id==='npu-create' || form?.id==='npu-password')) {
      const input = form.elements.password;
      input.disabled = event.target.checked;
      input.required = !event.target.checked;
      input.parentElement.hidden = event.target.checked;
      if (!event.target.checked) input.focus();
    }
    if (form?.id === 'npu-release') {
      readRelease(new FormData(form));
      form.querySelector('.release-fields').hidden = releaseEditor.mode !== 'partial';
      form.querySelector('#npu-release-preview').textContent = releasePreview();
      form.querySelector('button[type="submit"]').disabled = !releaseCandidate();
      patch({form:false});
    }
    if (form?.id === 'npu-reservation' && event.target.matches('select')) {
      draft = readDraft(new FormData(form));
      notice = null;
      root.querySelector('#npu-draft-time').textContent = draftDescription();
      if (view==='admin') {
        const reason = form.querySelector('[name="reason"]');
        const needed = !unlocking() && valid(draft) && lockGaps(draft).length > 0;
        reason.required = needed;
        reason.disabled = !needed;
        reason.parentElement.hidden = !needed;
      }
      patch({form:false});
    }
    if (form?.id === 'npu-access') {
      clearReview();
      root.querySelector('#npu-effective').textContent = effective(accounts[accountIndex],readAccess(new FormData(form)));
    }
  });

  root.addEventListener('input', event => {
    if (event.target.form?.id === 'npu-group') clearReview();
    if (event.target.form?.id==='npu-create' && event.target.name==='username') {
      event.target.form.elements.name.placeholder = event.target.value.trim() || '同登录账户';
    }
  });

  function readDraft(data) {
    return {m:draft.m,s:Number(data.get('start')),e:Number(data.get('end'))};
  }

  function closeLockDialog() {
    pendingConfirmation = null;
    lockDialog.close();
    lockDialog.innerHTML = '';
  }

  lockDialog.addEventListener('cancel', event => {
    event.preventDefault();
    closeLockDialog();
  });

  function showConfirmation(title, summary, details, label, commit) {
    pendingConfirmation = commit;
    lockDialog.innerHTML = '<div class="dialog-content"><h2 id="npu-lock-title">'+title+'</h2>'+
      '<p id="npu-lock-summary"><strong>'+escape(summary)+'</strong></p>'+details+
      '<div class="controls">'+button('cancel-lock','取消')+button('confirm-lock',label,'danger')+'</div></div>';
    lockDialog.showModal();
    lockDialog.querySelector('[data-action="cancel-lock"]').focus();
  }

  function reviewLock(candidate, reason, bookings) {
    const payload = {...candidate,reason,window:windowStart,revision};
    showConfirmation('强制释放预约并锁定？',machineLabel(candidate.m)+' · '+range(candidate),
      '<div class="review"><p>将强制释放 '+bookings.length+' 笔预约中与所选范围重叠的时段。</p><ul>'+
      bookings.map(r => {
        const owner = accounts.find(a => a.username === r.owner);
        const remainder = remainingRanges(r,candidate);
        return '<li>'+escape(owner?.name || '其他用户')+' · '+range(intersection(r,candidate))+
          (remainder.length?'<div class="small">保留 '+remainder.map(range).join('、')+'</div>':'')+'</li>';
      }).join('')+'</ul></div>'+
      '<p class="quiet">范围外的预约保留。确认后，所选范围全部锁定，范围内已有的锁定保留。</p>',
      '确认释放并锁定',() => save('lock',payload));
  }

  function reviewUnlock(candidate) {
    const locks = overlappingLocks(candidate);
    if (!locks.length) {
      say('选区内没有锁定，请选择包含锁定的时段。',true);
      patch({board:false,form:false});
      return;
    }
    const payload = {...candidate,window:windowStart,revision};
    showConfirmation('解除所选时段内的锁定？',machineLabel(candidate.m)+' · '+range(candidate),
      '<div class="review"><p>将解除以下时段的锁定。</p><ul>'+
      locks.map(r => '<li>'+range(intersection(r,candidate))+(r.reason?' · '+escape(r.reason):'')+'</li>').join('')+
      '</ul></div><p class="quiet">范围外的锁定及已有预约保留。解锁后，释放的时段可重新预约。</p>',
      '确认解锁',() => save('unlock',payload));
  }

  function remainingRanges(record, candidate) {
    const cut = intersection(record,candidate);
    const remainder = [];
    if (record.s < cut.s) remainder.push({...record,e:cut.s});
    if (cut.e < record.e) remainder.push({...record,s:cut.e});
    return remainder;
  }

  async function request(action, body) {
    const response = await fetch('/api/'+action, body === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json','X-NPU-Request':'1'}, body:JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || '服务暂时不可用，请稍后重试。');
      error.status = response.status;
      error.code = data.error;
      throw error;
    }
    return data;
  }

  function applyState(state) {
    const account = accounts[accountIndex]?.id;
    const group = groups[groupIndex]?.id;
    ({user,groups,accounts,administrators = [],records,upcoming,pastEnd,cutoff,revision,window:windowStart} = state);
    machines = Object.fromEntries(state.machines.map(machine => [machine.id,machine]));
    accountIndex = Math.max(0,accounts.findIndex(item => item.id===account));
    groupIndex = Math.max(0,groups.findIndex(item => item.id===group));
    view = user.role;
  }

  function signedOut(message) {
    if (lockDialog.open) closeLockDialog();
    reset();
    user = null;
    view = 'login';
    section = 'access';
    if (message) say(message,true);
    render();
  }

  function onBoard() {
    return view==='user' || (view==='admin' && section==='locks');
  }

  async function run(action, form) {
    if (busy) return;
    busy = true;
    root.setAttribute('aria-busy','true');
    const submit = form?.querySelector('button[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      await action();
    } catch (error) {
      if (error.code==='login_required') {
        signedOut('会话已过期，请重新登录。');
      } else if (['occupied','state_changed'].includes(error.code)) {
        try {
          applyState(await request('state'));
          reset();
          say(error.code==='occupied' ? error.message+' 时间轴已更新。' : error.message,true);
          if (onBoard()) patch();
          else render();
        } catch (refreshError) {
          if (refreshError.code==='login_required') signedOut('会话已过期，请重新登录。');
          else showError('操作未完成，暂时无法刷新数据。请稍后重试。',form);
        }
      } else {
        showError(error.status ? error.message : '无法连接服务，请稍后重试。',form);
      }
    } finally {
      busy = false;
      root.removeAttribute('aria-busy');
      if (submit?.isConnected) submit.disabled = form.id==='npu-release' && !releaseCandidate();
    }
  }

  function showError(message, form) {
    say(message,true);
    const target = form?.querySelector('.create-notice, [data-login-notice]');
    if (target) target.innerHTML = noticeHTML();
    else if (onBoard()) patch({board:false,form:false});
    else workspace.querySelector('[aria-live]').innerHTML = noticeHTML();
  }

  function save(action, payload, form) {
    return run(async () => {
      const {state,message,generatedPassword} = await request(action,payload);
      if (form?.id==='npu-password') closeLockDialog();
      applyState(state);
      if (action==='create-account') accountIndex = accounts.length-1;
      if (action==='create-group') groupIndex = groups.length-1;
      reset();
      say(message);
      if (onBoard()) patch();
      else render();
      if (generatedPassword) showPasswordValue('自动生成的密码','请复制后私聊告知对应账户。',generatedPassword);
    },form);
  }

  root.addEventListener('submit', event => {
    event.preventDefault();
    if (busy) return;
    const form = event.target;
    const data = new FormData(form);
    if (form.id==='npu-login') {
      run(async () => {
        applyState(await request('login',Object.fromEntries(data)));
        reset();
        render();
      },form);
    } else if (form.id==='npu-password') {
      const id = Number(form.dataset.target);
      const payload = passwordPayload(data);
      if (id) save('reset-password',{...payload,id},form);
      else save('change-password',payload,form);
    } else if (form.id==='npu-release') {
      const record = releaseEditor?.record;
      if (view!=='user' || !ownedBookings(user).includes(record) || !machines[record.m]) return;
      readRelease(data);
      const candidate = releaseCandidate();
      if (candidate) save('release',{...candidate,id:record.id,window:windowStart},form);
    } else if (form.id==='npu-reservation') {
      const candidate = readDraft(data);
      if (!valid(candidate)) {
        showError(view==='admin'?'请选择有效的起止时间。':'所选范围无效或包含占用时段，请调整起止时间。',form);
        return;
      }
      const payload = {...candidate,window:windowStart,revision};
      if (view==='user') {
        save('book',payload,form);
      } else if (unlocking()) {
        reviewUnlock(candidate);
      } else {
        const reason = (data.get('reason') || '').trim();
        if (lockGaps(candidate).length && !reason) {
          showError('请输入锁定原因。',form);
          return;
        }
        const bookings = overlappingBookings(candidate);
        if (bookings.length) reviewLock(candidate,reason,bookings);
        else save('lock',{...payload,reason},form);
      }
    } else if (form.id==='npu-add-machine') {
      save('add-machine',Object.fromEntries(data),form);
    } else if (form.id==='npu-access') {
      const account = accounts[accountIndex];
      const next = readAccess(data);
      const impact = affectedBookings(account,allowed(next)).map(record => ({account,record}));
      const payload = {...next,id:account.id,revision};
      reviewEdit(form,'保存权限',impact,() => save('set-access',payload,form));
    } else if (form.id==='npu-create') {
      const name = data.get('name');
      if (form.dataset.kind==='account') {
        save('create-account',{...passwordPayload(data),name,username:data.get('username')},form);
      } else {
        save('create-group',{name},form);
      }
    } else if (form.id==='npu-group') {
      const name = data.get('name');
      const members = data.getAll('member').map(Number);
      const next = groups.map((g,i) => i===groupIndex ? {...g,members} :
        {...g,members:g.members.filter(m=>!members.includes(m))});
      const impact = accounts.flatMap(account =>
        affectedBookings(account,allowed(account,next)).map(record => ({account,record})));
      const payload = {name,members,id:groups[groupIndex].id,revision};
      reviewEdit(form,'保存分组',impact,() => save('edit-group',payload,form));
    }
  });

  async function start() {
    busy = true;
    try {
      applyState(await request('state'));
      render();
    } catch (error) {
      signedOut(error.status===401 ? '' : '无法加载服务，请登录重试。');
    } finally {
      busy = false;
    }
  }

  async function pollBoard() {
    if (busy || polling || !onBoard() || draft || releaseEditor || drag || lockDialog.open) return;
    const before = user;
    polling = true;
    try {
      const state = await request('state');
      if (before===user && onBoard() && !busy && !draft && !releaseEditor && !drag && !lockDialog.open &&
          (state.revision!==revision || state.window!==windowStart || state.pastEnd!==pastEnd || state.cutoff!==cutoff)) {
        applyState(state);
        patch();
      }
    } catch (error) {
      if (before===user && !busy && error.code==='login_required') signedOut('会话已过期，请重新登录。');
    } finally {
      polling = false;
    }
  }

  function slotAt(event, track) {
    const rect = track.getBoundingClientRect();
    return Math.max(0,Math.min(47,Math.floor((event.clientX-rect.left)/rect.width*48)));
  }

  root.addEventListener('pointerdown', event => {
    const track = event.target.closest('[data-track]');
    if (busy || !track || event.button !== 0 || !event.isPrimary) return;
    drag = {track, id:event.pointerId, s:slotAt(event,track), preview:null};
    track.setPointerCapture(event.pointerId);
  });
  root.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId || event.pointerType === 'touch') return;
    const end = slotAt(event,drag.track);
    const candidate = {m:Number(drag.track.dataset.track),s:Math.min(drag.s,end),e:Math.max(drag.s,end)+1};
    drag.preview?.remove();
    drag.preview = null;
    const own = ownRange(candidate);
    if (!own && !valid(candidate)) return;
    drag.track.insertAdjacentHTML('beforeend',own ? block(candidate,'release-draft','') : draftBlock(candidate));
    drag.preview = drag.track.lastElementChild;
  });
  root.addEventListener('pointerup', event => {
    if (!drag || drag.id !== event.pointerId) return;
    const {track,s,preview} = drag;
    const end = slotAt(event,track);
    drag = null;
    preview?.remove();
    const candidate = {m:Number(track.dataset.track),s:Math.min(s,end),e:Math.max(s,end)+1};
    const own = ownRange(candidate);
    if (own) beginRelease(own,s===end ? null : candidate);
    else select(candidate);
  });
  root.addEventListener('pointercancel', event => {
    if (drag?.id !== event.pointerId) return;
    drag.preview?.remove();
    drag = null;
  });
  start();
  setInterval(pollBoard,30000);
})();
