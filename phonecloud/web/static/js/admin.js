'use strict';

// 管理页：首次初始化、账号、空间与授权、磁盘概览。
(function () {
  const { h, icon, api, fmtSize, fmtTime, toast } = PC;
  const main = document.getElementById('main');
  const topbar = document.getElementById('topbar');

  const TABS = [
    { id: 'overview', label: '概览', render: renderHome },
    { id: 'spaces', label: '空间', render: renderSpaces },
  ];
  const state = { user: null, tab: 'overview' };
  let viewSeq = 0;

  // ---- 启动 ----

  async function boot() {
    viewSeq += 1;
    topbar.hidden = true;
    let status;
    try {
      status = await api('GET', '/admin/api/state');
      if (status.needs_setup) return showSetup(status);
      const me = await api('GET', '/api/auth/me', undefined, { quiet401: true });
      state.user = me.user;
    } catch (err) {
      if (err.status === 401) return showLogin();
      return showMessage(err.message);
    }
    if (!state.user.is_admin) return showNotAdmin();
    renderTopbar();
    showTab(state.tab);
  }

  function showMessage(text) {
    main.replaceChildren(h('div', { class: 'empty' }, text));
  }

  function authCard(title, intro, buttonText, onSubmit, note) {
    const username = h('input', { type: 'text', autocomplete: 'username', autocapitalize: 'none', required: true });
    const password = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    const error = h('p', { class: 'form-error', role: 'alert' });
    const submit = h('button', { class: 'btn primary', type: 'submit' }, buttonText);
    const form = h(
      'form',
      {
        class: 'card auth',
        onsubmit: async (event) => {
          event.preventDefault();
          submit.disabled = true;
          error.textContent = '';
          try {
            await onSubmit(username.value, password.value);
          } catch (err) {
            error.textContent = err.message;
            submit.disabled = false;
          }
        },
      },
      h('h1', null, h('img', { src: '/static/icon.svg', alt: '' }), title),
      h('p', { class: 'muted' }, intro),
      h('label', null, '账号', username),
      h('label', null, '密码', password),
      error,
      submit,
      note && h('p', { class: 'muted small' }, note)
    );
    main.replaceChildren(form);
    username.focus();
  }

  function showSetup(status) {
    if (!status.setup_allowed) {
      main.replaceChildren(
        h(
          'div',
          { class: 'card auth' },
          h('h1', null, h('img', { src: '/static/icon.svg', alt: '' }), '尚未初始化'),
          h('p', null, 'PhoneCloud 还没有管理员账号。为了安全，首次初始化只能在运行 PhoneCloud 的那台电脑上进行。'),
          h('p', { class: 'muted' }, '请在那台电脑的浏览器里打开：'),
          h('div', { class: 'code' }, 'http://127.0.0.1:' + status.port + '/admin/')
        )
      );
      return;
    }
    authCard(
      '创建管理员',
      '首次使用，请先创建管理员账号。管理员用来创建家人账号和共享文件夹。',
      '创建并登录',
      async (username, password) => {
        await api('POST', '/admin/api/setup', { username, password });
        await api('POST', '/api/auth/login', { username, password }, { quiet401: true });
        boot();
      },
      '账号和密码至少 1 位。'
    );
  }

  function showLogin(message) {
    viewSeq += 1;
    state.user = null;
    topbar.hidden = true;
    authCard('管理员登录', message || '请使用管理员账号登录。', '登录', async (username, password) => {
      await api('POST', '/api/auth/login', { username, password }, { quiet401: true });
      boot();
    });
  }

  function showNotAdmin() {
    main.replaceChildren(
      h(
        'div',
        { class: 'card auth' },
        h('h1', null, '没有管理权限'),
        h('p', null, '当前登录的账号 ' + state.user.username + ' 不是管理员。'),
        h('a', { class: 'btn primary', href: '/' }, '返回用户端'),
        h('p', null),
        h('button', { class: 'btn', type: 'button', onclick: logout }, '换管理员账号登录')
      )
    );
  }

  async function logout() {
    try {
      await api('POST', '/api/auth/logout');
    } catch (err) {
      if (err.offline) return;
    }
    showLogin();
  }

  function renderTopbar() {
    topbar.hidden = false;
    topbar.replaceChildren(
      h('a', { class: 'brand', href: '/admin/' }, h('img', { src: '/static/icon.svg', alt: '' }), 'PhoneCloud 管理'),
      h('span', { class: 'spacer' }),
      h('span', { class: 'who' }, state.user.username),
      h('a', { class: 'btn small', href: '/' }, '用户端'),
      h('button', { class: 'btn small', type: 'button', onclick: logout }, '退出')
    );
  }

  // ---- 标签页 ----

  async function showTab(id) {
    state.tab = id;
    const seq = ++viewSeq;
    const tabs = h(
      'div',
      { class: 'tabs', role: 'tablist' },
      TABS.map((tab) =>
        h(
          'button',
          {
            class: 'tab' + (tab.id === id ? ' active' : ''),
            type: 'button',
            role: 'tab',
            'aria-selected': String(tab.id === id),
            onclick: () => showTab(tab.id),
          },
          tab.label
        )
      )
    );
    const body = h('div', null, h('div', { class: 'empty' }, '加载中…'));
    main.replaceChildren(tabs, body);
    try {
      const content = await TABS.find((tab) => tab.id === id).render();
      if (seq === viewSeq) body.replaceChildren(...content);
    } catch (err) {
      if (seq === viewSeq && err.status !== 401) body.replaceChildren(h('div', { class: 'notice error' }, err.message));
    }
  }

  const reload = () => showTab(state.tab);

  /** 执行一个管理操作：成功后提示并刷新当前标签页，失败时提示原因。 */
  async function act(promise, successText) {
    try {
      await promise;
    } catch (err) {
      if (err.status !== 401) toast(err.message, 'error');
      return false;
    }
    if (successText) toast(successText, 'ok');
    reload();
    return true;
  }

  // ---- 概览 ----

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('地址已复制', 'ok');
    } catch (err) {
      toast('复制失败，请手动选中地址复制', 'error');
    }
  }

  function buildAccess(data) {
    const svgs = data.qr_svgs && data.qr_svgs.length ? data.qr_svgs : data.qr_svg ? [data.qr_svg] : [];
    const codes = data.urls.map((url, i) => ({ url, svg: svgs[i] })).filter((item) => item.svg);
    let qrIndex = 0;
    const qr = h('div', { class: 'qr' });
    const qrUrl = h('a', { class: 'qr-url' });
    const copyBtn = h(
      'button',
      {
        class: 'qr-copy',
        type: 'button',
        title: '复制地址',
        'aria-label': '复制地址',
        onclick: () => copyText(codes[qrIndex].url),
      },
      icon('copy')
    );
    const qrNext = h('button', { class: 'btn', type: 'button', onclick: () => rotate() }, '换一个');

    function showQr(index) {
      qrIndex = index;
      const item = codes[index];
      qr.innerHTML = item.svg;
      qrUrl.href = item.url;
      qrUrl.textContent = item.url;
    }
    if (codes.length) showQr(0);

    async function rotate() {
      // 地址看完一轮后，再点一次就重新读取网卡，不再单独放刷新按钮
      if (codes.length > 1 && qrIndex < codes.length - 1) {
        showQr(qrIndex + 1);
        return;
      }
      qrNext.disabled = true;
      qrNext.textContent = '正在刷新…';
      try {
        section.replaceWith(buildAccess(await api('GET', '/admin/api/overview')));
      } catch (err) {
        qrNext.disabled = false;
        qrNext.textContent = '换一个';
        if (err.status !== 401) toast(err.message, 'error');
      }
    }

    const section = h(
      'section',
      { class: 'card access-card' },
      h('h2', null, '手机怎么打开'),
      codes.length
        ? h(
            'div',
            { class: 'qr-block' },
            qr,
            h(
              'div',
              { class: 'qr-side' },
              h('div', { class: 'qr-current' }, h('span', { class: 'qr-url-wrap' }, qrUrl, copyBtn), qrNext),
              h('p', { class: 'muted small' }, '用手机相机扫这个码，即可打开。'),
              h('p', { class: 'muted small' }, '用浏览器打开后，选添加到主屏幕。地址变了就再扫一次管理页上的二维码。')
            )
          )
        : h(
            'div',
            null,
            h('div', { class: 'notice' }, '没有检测到局域网地址，请确认电脑已连接网络。'),
            qrNext
          ),
      h(
        'details',
        { class: 'fold' },
        h('summary', null, '手机打不开？'),
        h('p', { class: 'muted small' }, '通常是 Windows 防火墙拦了端口。用管理员身份打开命令提示符，执行：'),
        h('div', { class: 'code' }, data.firewall_command),
        h('p', { class: 'muted small' }, '这条规则只对“专用网络”生效。如果网络被标成了“公用”，再用管理员执行：'),
        h('div', { class: 'code' }, data.private_network_command)
      )
    );
    return section;
  }

  async function renderOverview() {
    const data = await api('GET', '/admin/api/overview');
    const access = buildAccess(data);

    const disks = data.disks.map((disk) => {
      if (disk.error) {
        return h('div', { class: 'disk' }, h('div', { class: 'head' }, h('strong', null, disk.disk), h('span', { class: 'badge danger' }, disk.error)), usesList(disk));
      }
      const ratio = disk.total ? disk.used / disk.total : 0;
      const bar = h('div');
      bar.style.width = (ratio * 100).toFixed(1) + '%';
      const level = ratio > 0.95 ? ' danger' : ratio > 0.85 ? ' warn' : '';
      return h(
        'div',
        { class: 'disk' },
        h(
          'div',
          { class: 'head' },
          h('strong', null, disk.disk),
          h('span', { class: 'muted small' }, '剩余 ' + fmtSize(disk.free) + ' / 共 ' + fmtSize(disk.total) + '（已用 ' + Math.round(ratio * 100) + '%）')
        ),
        h('div', { class: 'progress' + level }, bar),
        usesList(disk)
      );
    });

    const machine = h(
      'details',
      { class: 'card fold' },
      h('summary', null, '本机状态'),
      h('h3', null, '磁盘'),
      disks,
      h('h3', null, '运行信息'),
      h(
        'dl',
        { class: 'kv' },
        h('dt', null, '版本'), h('dd', null, data.version),
        h('dt', null, '主目录'), h('dd', null, h('code', null, data.home)),
        h('dt', null, '端口'), h('dd', null, data.port),
        h('dt', null, '单文件上限'), h('dd', null, fmtSize(data.max_upload_mb * 1024 * 1024)),
        h('dt', null, '磁盘保留空间'), h('dd', null, fmtSize(data.min_free_mb * 1024 * 1024))
      ),
      h('p', { class: 'muted small' }, '改端口或上限：编辑主目录下的 config.json，然后重启 PhoneCloud。')
    );
    return [access, machine];
  }

  async function renderHome() {
    const [overview, users] = await Promise.all([renderOverview(), renderUsers()]);
    const access = overview[0];
    const machine = overview[1];
    return [access, ...users, machine];
  }

  function usesList(disk) {
    return h('ul', { class: 'uses' }, disk.uses.map((use) => h('li', null, use)));
  }

  // ---- 账号 ----

  async function renderUsers() {
    const { users } = await api('GET', '/admin/api/users');

    const rows = users.map((user) => {
      const isSelf = user.id === state.user.id;
      return h(
        'tr',
        null,
        h('td', null, user.username, isSelf && h('span', { class: 'muted small' }, '（当前登录）')),
        h('td', null, user.is_admin ? h('span', { class: 'badge primary' }, '管理员') : h('span', { class: 'badge' }, '普通账号')),
        h('td', null, user.disabled ? h('span', { class: 'badge danger' }, '已禁用') : h('span', { class: 'badge ok' }, '正常')),
        h('td', { class: 'muted small' }, fmtTime(user.created_at)),
        h(
          'td',
          { class: 'actions' },
          h('button', { class: 'btn small', type: 'button', onclick: () => passwordDialog(user) }, '重置密码'),
          h(
            'button',
            {
              class: 'btn small danger',
              type: 'button',
              disabled: isSelf || user.has_files,
              title: isSelf
                ? '不能删除当前登录的账号'
                : user.has_files
                  ? '文件夹里还有文件，清空后才能删除'
                  : null,
              onclick: () => {
                if (confirm('删除 ' + user.username + ' 后不能再登录。只去掉空的账号文件夹，里面还有文件时删不掉。确定删除吗？')) {
                  act(api('DELETE', '/admin/api/users/' + user.id), '已删除 ' + user.username);
                }
              },
            },
            '删除'
          ),
          user.disabled
            ? h('button', { class: 'btn small', type: 'button', onclick: () => act(api('POST', '/admin/api/users/' + user.id + '/enable'), '已启用 ' + user.username) }, '启用')
            : h(
                'button',
                {
                  class: 'btn small danger',
                  type: 'button',
                  disabled: isSelf,
                  title: isSelf ? '不能禁用当前登录的账号' : null,
                  onclick: () => {
                    if (confirm('禁用后 ' + user.username + ' 会立即被登出且无法登录，已上传的文件不受影响。确定禁用吗？')) {
                      act(api('POST', '/admin/api/users/' + user.id + '/disable'), '已禁用 ' + user.username);
                    }
                  },
                },
                '禁用'
              )
        )
      );
    });

    return [
      h(
        'section',
        { class: 'card' },
        h(
          'div',
          { class: 'section-head' },
          h('h2', null, '账号列表'),
          h('button', { class: 'btn primary small', type: 'button', onclick: createUserDialog }, '创建账号')
        ),
        h(
          'div',
          { class: 'table-wrap' },
          h('table', null,
            h('thead', null, h('tr', null, h('th', null, '账号'), h('th', null, '角色'), h('th', null, '状态'), h('th', null, '创建时间'), h('th', null))),
            h('tbody', null, rows))
        )
      ),
    ];
  }

  function createUserDialog() {
    const username = h('input', { type: 'text', autocomplete: 'off', autocapitalize: 'none', required: true, maxlength: 32 });
    const password = h('input', { type: 'text', autocomplete: 'off', required: true });
    const error = h('p', { class: 'form-error', role: 'alert' });
    const dialog = h('dialog');
    const close = () => {
      dialog.close();
      dialog.remove();
    };
    dialog.append(
      h(
        'form',
        {
          onsubmit: async (event) => {
            event.preventDefault();
            try {
              await api('POST', '/admin/api/users', { username: username.value.trim(), password: password.value });
            } catch (err) {
              error.textContent = err.message;
              return;
            }
            close();
            toast('账号已创建', 'ok');
            reload();
          },
        },
        h('h2', null, '创建账号'),
        h('label', null, '账号名', username),
        h('label', null, '初始密码', password),
        h('p', { class: 'muted small' }, '文件夹要另外建，别人默认可看，但不能上传。'),
        error,
        h(
          'div',
          { class: 'buttons' },
          h('button', { class: 'btn', type: 'button', onclick: close }, '取消'),
          h('button', { class: 'btn primary', type: 'submit' }, '创建')
        )
      )
    );
    dialog.addEventListener('cancel', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
    username.focus();
  }

  function passwordDialog(user) {
    const password = h('input', { type: 'text', autocomplete: 'off', required: true });
    const error = h('p', { class: 'form-error', role: 'alert' });
    const dialog = h('dialog');
    const close = () => {
      dialog.close();
      dialog.remove();
    };
    dialog.append(
      h(
        'form',
        {
          onsubmit: async (event) => {
            event.preventDefault();
            try {
              await api('POST', '/admin/api/users/' + user.id + '/password', { password: password.value });
            } catch (err) {
              error.textContent = err.message;
              return;
            }
            close();
            toast(user.username + ' 的密码已重置，该账号在其他设备上的登录已失效', 'ok');
          },
        },
        h('h2', null, '重置 ' + user.username + ' 的密码'),
        h('label', null, '新密码', password),
        error,
        h(
          'div',
          { class: 'buttons' },
          h('button', { class: 'btn', type: 'button', onclick: close }, '取消'),
          h('button', { class: 'btn primary', type: 'submit' }, '确定')
        )
      )
    );
    dialog.addEventListener('cancel', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  function othersOf(space, users) {
    return users.filter((user) => user.id !== space.owner_user_id);
  }

  /** 勾选表示“能看”。all 模式下名单是不能看的人，only 模式下名单是能看的人。 */
  function canSeeNow(space, userId) {
    const listed = space.grants.some((grant) => grant.user_id === userId);
    return space.visibility === 'all' ? !listed : listed;
  }

  function audienceText(space, users) {
    const names = othersOf(space, users).filter((user) => canSeeNow(space, user.id)).map((user) => user.username);
    if (space.visibility === 'all' && names.length === othersOf(space, users).length) return '所有人';
    if (space.visibility === 'all') {
      const hidden = othersOf(space, users).filter((user) => !canSeeNow(space, user.id)).map((user) => user.username);
      return hidden.length ? '所有人，' + hidden.join('、') + ' 除外' : '所有人';
    }
    if (space.kind === 'personal') return names.length ? space.owner + '、' + names.join('、') : '只有 ' + space.owner;
    return names.length ? names.join('、') : '还没有人';
  }

  // ---- 空间 ----

  async function renderSpaces() {
    const [{ spaces, default_root: defaultRoot }, { users }] = await Promise.all([
      api('GET', '/admin/api/spaces'),
      api('GET', '/admin/api/users'),
    ]);

    const tree = buildSpaceTree(spaces, defaultRoot);

    return [
      h(
        'section',
        { class: 'card' },
        h(
          'div',
          { class: 'section-head' },
          h('h2', null, '已有的'),
          h('button', { class: 'btn primary small', type: 'button', onclick: () => createFolderDialog(defaultRoot) }, '创建')
        ),
        tree.length ? h('div', { class: 'tree' }, tree.map((node) => renderTreeNode(node, users))) : h('div', { class: 'empty' }, '还没有。点「创建」加一个。')
      ),
    ];
  }

  function createFolderDialog(defaultRoot) {
    const name = h('input', {
      type: 'text',
      autocomplete: 'off',
      required: true,
      maxlength: 50,
      placeholder: '例如：家庭相册',
    });
    const root = h('input', { type: 'text', autocomplete: 'off', required: true, value: defaultRoot, spellcheck: 'false' });
    const error = h('p', { class: 'form-error', role: 'alert' });
    const dialog = h('dialog', { class: 'wide' });
    const close = () => {
      dialog.close();
      dialog.remove();
    };
    dialog.append(
      h(
        'form',
        {
          onsubmit: async (event) => {
            event.preventDefault();
            const folderName = name.value.trim();
            const base = root.value.trim().replace(/^"|"$/g, '').replace(/[\\/]+$/, '');
            const home = String(defaultRoot).replace(/[\\/]+$/, '');
            if (!base) {
              error.textContent = '请填写根目录';
              return;
            }
            // 默认根目录时把路径留给服务端，仍建在主目录下的同名文件夹。
            const path = base.toLowerCase() === home.toLowerCase() ? '' : base + '\\' + folderName;
            try {
              await api('POST', '/admin/api/spaces', { name: folderName, path: path });
            } catch (err) {
              error.textContent = err.message;
              return;
            }
            close();
            toast('已创建，所有人都能看', 'ok');
            reload();
          },
        },
        h('h2', null, '创建文件夹'),
        h('label', null, '名称', name),
        h('label', null, '根目录', root),
        h('p', { class: 'muted small' }, '会建在根目录下，和名称同名。主目录里没有的会自动新建。换到其他位置时，请先在电脑上建好这个文件夹。'),
        error,
        h(
          'div',
          { class: 'buttons' },
          h('button', { class: 'btn', type: 'button', onclick: close }, '取消'),
          h('button', { class: 'btn primary', type: 'submit' }, '创建')
        )
      )
    );
    dialog.addEventListener('cancel', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
    name.focus();
  }

  function pathSegments(space, root) {
    const full = String(space.path || '').replace(/[\\/]+$/, '');
    const base = String(root || '').replace(/[\\/]+$/, '');
    let relative = full;
    if (base && full.toLowerCase().startsWith(base.toLowerCase())) {
      relative = full.slice(base.length).replace(/^[\\/]+/, '');
    }
    let parts = relative.split(/[\\/]+/).filter(Boolean);
    // accounts 只是存放各账号目录的那一层，树上直接从账号名开始。
    if (parts.length > 1 && parts[0].toLowerCase() === 'accounts') parts = parts.slice(1);
    return parts.length ? parts : [space.name || full || '未命名'];
  }

  function buildSpaceTree(spaces, root) {
    const top = [];
    const index = new Map();
    function ensure(parent, key) {
      const id = parent === top ? key : parent._key + '\0' + key;
      if (!index.has(id)) {
        const node = { name: key, _key: id, children: [], space: null };
        index.set(id, node);
        parent.push ? parent.push(node) : parent.children.push(node);
      }
      return index.get(id);
    }
    spaces.forEach((space) => {
      const parts = pathSegments(space, root);
      let parent = top;
      parts.forEach((part, i) => {
        const node = ensure(parent, part);
        if (i === parts.length - 1) node.space = space;
        parent = node;
      });
    });
    const sortNodes = (nodes) => {
      nodes.sort((a, b) => a.name.localeCompare(b.name, 'zh'));
      nodes.forEach((node) => sortNodes(node.children));
    };
    sortNodes(top);
    return top;
  }

  function renderTreeNode(node, users) {
    const space = node.space;
    const personal = space && space.kind === 'personal';
    const kids = h('div', { class: 'tree-children', hidden: true });
    node.children.forEach((child) => kids.append(renderTreeNode(child, users)));
    const toggle = h('button', {
      class: 'tree-toggle' + (node.children.length ? '' : ' is-leaf'),
      type: 'button',
      'aria-expanded': 'false',
      'aria-label': node.children.length ? '展开 ' + node.name : null,
      disabled: !node.children.length,
      onclick: () => {
        const open = kids.hidden;
        kids.hidden = !open;
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.setAttribute('aria-label', (open ? '收起 ' : '展开 ') + node.name);
      },
    });
    const row = h(
      'div',
      { class: 'tree-row' },
      toggle,
      h('span', { class: 'name', title: space ? space.path : node.name }, node.name),
      space && h('span', { class: 'badge' }, personal ? '上传区' : '文件夹'),
      space && !space.exists && h('span', { class: 'muted small warn-text' }, '文件夹不存在'),
      space && h('span', { class: 'spacer' }),
      space && h('a', { class: 'btn small', href: browseHref(space) }, '查看文件'),
      space && h('button', { class: 'btn small', type: 'button', onclick: () => audienceDialog(space, users) }, '谁可以看'),
      space &&
        !personal &&
        h(
          'button',
          {
            class: 'btn small danger',
            type: 'button',
            onclick: () => {
              if (confirm('删除“' + space.name + '”？电脑上的文件不会被删除。')) {
                act(api('DELETE', '/admin/api/spaces/' + space.id), '已删除');
              }
            },
          },
          '删除'
        )
    );
    const meta = space ? h('div', { class: 'tree-meta' }, '谁可以看：' + audienceText(space, users)) : null;
    return h('div', { class: 'tree-node' }, row, meta, node.children.length ? kids : null);
  }

  function browseHref(space) {
    return '/#/s/' + space.id;
  }

  function audienceDialog(space, users) {
    const others = othersOf(space, users);
    const personal = space.kind === 'personal';
    let mode = space.visibility === 'only' ? 'only' : 'all';
    const picked = new Set(others.filter((user) => canSeeNow(space, user.id)).map((user) => user.id));
    const error = h('p', { class: 'form-error', role: 'alert' });
    const list = h('div', { class: 'check-list' });
    const dialog = h('dialog', { class: 'wide' });

    function paint() {
      if (!others.length) {
        list.replaceChildren(h('p', { class: 'muted small' }, '还没有其他账号。先在「概览」里创建。'));
        return;
      }
      list.replaceChildren(
        ...others.map((user) => {
          const box = h('input', { type: 'checkbox', checked: picked.has(user.id) });
          box.addEventListener('change', () => {
            if (box.checked) picked.add(user.id);
            else picked.delete(user.id);
          });
          return h('label', { class: 'check' }, box, user.username + (user.disabled ? '（已禁用）' : ''));
        })
      );
    }

    const all = h('input', { type: 'radio', name: 'audience', checked: mode === 'all' });
    const only = h('input', { type: 'radio', name: 'audience', checked: mode === 'only' });
    all.addEventListener('change', () => {
      mode = 'all';
    });
    only.addEventListener('change', () => {
      mode = 'only';
    });

    const save = h('button', { class: 'btn primary', type: 'submit' }, '保存');
    const close = () => {
      dialog.close();
      dialog.remove();
    };
    dialog.append(
      h(
        'form',
        {
          onsubmit: async (event) => {
            event.preventDefault();
            save.disabled = true;
            error.textContent = '';
            try {
              await saveAudience(space, users, mode, picked);
            } catch (err) {
              error.textContent = err.message;
              save.disabled = false;
              return;
            }
            close();
            toast('谁可以看已更新', 'ok');
            reload();
          },
        },
        h('h2', null, (personal ? space.owner + ' 的上传' : space.name) + ' · 谁可以看'),
        h('p', { class: 'muted small' }, personal ? '本人始终能看，也只有本人能上传。' : '能看这个相册的人可以上传。'),
        h('label', { class: 'check' }, all, '所有人都能看'),
        h('label', { class: 'check' }, only, '仅勾选的人能看'),
        h('p', { class: 'muted small' }, '勾上的人能看，取消勾选的人看不到。'),
        list,
        error,
        h('div', { class: 'buttons' }, h('button', { class: 'btn', type: 'button', onclick: close }, '取消'), save)
      )
    );
    paint();
    dialog.addEventListener('cancel', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  /** 把对话框里的勾选写成现有的可见范围和名单。勾选始终表示“能看”。 */
  async function saveAudience(space, users, mode, picked) {
    const grantIds = othersOf(space, users)
      .filter((user) => (mode === 'all' ? !picked.has(user.id) : picked.has(user.id)))
      .map((user) => user.id);
    if (space.visibility !== mode) {
      await api('POST', '/admin/api/spaces/' + space.id + '/visibility', { visibility: mode });
      for (const userId of grantIds) await addGrant(space.id, userId, mode);
      return;
    }
    const have = new Map(space.grants.map((grant) => [grant.user_id, grant.id]));
    const want = new Set(grantIds);
    for (const [userId, grantId] of have) {
      if (!want.has(userId)) await api('DELETE', '/admin/api/grants/' + grantId);
    }
    for (const userId of want) {
      if (!have.has(userId)) await addGrant(space.id, userId, mode);
    }
  }

  async function addGrant(spaceId, userId, mode) {
    try {
      await api('POST', '/admin/api/grants', { space_id: spaceId, user_id: userId, visibility: mode });
    } catch (err) {
      if (err.status !== 409) throw err;
    }
  }

  // ---- 事件 ----

  PC.onUnauthorized = () => showLogin('登录已过期，请重新登录。');
  PC.onReconnect = () => {
    if (state.user) reload();
    else if (!main.querySelector('form')) boot();
  };

  boot();
})();
