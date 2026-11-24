'use strict';

// 用户端：登录、空间列表、只读浏览、图片预览、视频播放、下载、单文件上传。
(function () {
  const { h, icon, api, fmtSize, fmtTime, toast } = PC;
  const main = document.getElementById('main');
  const topbar = document.getElementById('topbar');

  const BATCH = 120; // 大目录分批渲染，滚动到底再加载下一批
  const UPLOAD_HINT = '大文件请用稳定的 Wi‑Fi，中断后需要重新上传。';

  const state = {
    user: null,
    listing: null, // 当前目录：{ space, path, entries }
    uploading: false,
  };
  let navSeq = 0; // 每次导航递增；晚到的旧请求结果直接丢弃
  let overlay = null;

  // ---- 地址 ----

  function spaceHash(spaceId, path) {
    const tail = path ? '/' + path.split('/').map(encodeURIComponent).join('/') : '';
    return '#/s/' + spaceId + tail;
  }

  function parseHash() {
    let parts;
    try {
      parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
    } catch (err) {
      return { view: 'spaces' };
    }
    if (parts[0] === 's' && /^\d+$/.test(parts[1] || '')) {
      return { view: 'browse', space: Number(parts[1]), path: parts.slice(2).join('/') };
    }
    return { view: 'spaces' };
  }

  function fileUrl(kind, entry) {
    const { space, path } = state.listing;
    const full = path ? path + '/' + entry.name : entry.name;
    let url = '/api/fs/' + kind + '?space=' + space.id + '&path=' + encodeURIComponent(full);
    // 缩略图地址带上修改时间和大小：文件变了地址就变，浏览器缓存不会显示旧图
    if (kind === 'thumb') url += '&v=' + entry.mtime + '-' + entry.size;
    return url;
  }

  // ---- 启动 / 登录 ----

  async function boot() {
    closeOverlay();
    try {
      const me = await api('GET', '/api/auth/me', undefined, { quiet401: true });
      state.user = me.user;
    } catch (err) {
      if (err.status === 401) return showLogin();
      return showProblem(err.message, null, err.offline);
    }
    renderTopbar();
    route();
  }

  function renderTopbar() {
    topbar.hidden = false;
    const items = [
      h('a', { class: 'brand', href: '#/' }, h('img', { src: '/static/icon.svg', alt: '' }), 'PhoneCloud'),
      h('span', { class: 'spacer' }),
      h('span', { class: 'who', title: '当前账号' }, state.user.username),
    ];
    if (state.user.is_admin) items.push(h('a', { class: 'btn small', href: '/admin/' }, '管理'));
    items.push(h('button', { class: 'btn small', type: 'button', onclick: logout }, '退出'));
    topbar.replaceChildren(...items);
  }

  function showLogin(message) {
    navSeq += 1;
    closeOverlay();
    state.user = null;
    state.listing = null;
    topbar.hidden = true;
    const username = h('input', { type: 'text', name: 'username', autocomplete: 'username', autocapitalize: 'none', required: true });
    const password = h('input', { type: 'password', name: 'password', autocomplete: 'current-password', required: true });
    const error = h('p', { class: 'form-error', role: 'alert' }, message || '');
    const submit = h('button', { class: 'btn primary', type: 'submit' }, '登录');
    const form = h(
      'form',
      {
        class: 'card auth',
        onsubmit: async (event) => {
          event.preventDefault();
          submit.disabled = true;
          error.textContent = '';
          try {
            const result = await api(
              'POST',
              '/api/auth/login',
              { username: username.value, password: password.value },
              { quiet401: true }
            );
            state.user = result.user;
            renderTopbar();
            route();
          } catch (err) {
            error.textContent = err.message;
            submit.disabled = false;
          }
        },
      },
      h('h1', null, h('img', { src: '/static/icon.svg', alt: '' }), 'PhoneCloud'),
      h('p', { class: 'muted' }, '登录后可以浏览、预览和下载电脑上的文件。'),
      h('label', null, '账号', username),
      h('label', null, '密码', password),
      error,
      submit
    );
    main.replaceChildren(form);
    username.focus();
  }

  async function logout() {
    if (state.uploading && !confirm('有文件正在上传，退出会中断上传。确定退出吗？')) return;
    try {
      await api('POST', '/api/auth/logout');
    } catch (err) {
      if (err.offline) return; // 断线时保持原样，顶部已有提示
    }
    if (state.uploading) {
      state.uploading = false; // 已经确认过，刷新时不再弹"离开页面"的提示
      location.reload(); // 刷新会中断正在进行的上传
    } else {
      showLogin();
    }
  }

  // ---- 通用占位 ----

  function showLoading() {
    main.replaceChildren(h('div', { class: 'empty' }, '加载中…'));
  }

  function showProblem(message, backHref, offline) {
    main.replaceChildren(
      h(
        'div',
        { class: 'empty' },
        icon(offline ? 'offline' : 'folder'),
        h('p', null, message),
        backHref && h('a', { class: 'btn', href: backHref }, '返回')
      )
    );
  }

  // ---- 路由 ----

  function route() {
    if (!state.user) return;
    const target = parseHash();
    if (target.view === 'browse') showBrowser(target.space, target.path);
    else showSpaces();
  }

  // ---- 空间列表 ----

  async function showSpaces() {
    const seq = ++navSeq;
    state.listing = null;
    showLoading();
    let spaces;
    try {
      spaces = (await api('GET', '/api/spaces')).spaces;
    } catch (err) {
      if (seq === navSeq && err.status !== 401) showProblem(err.message, null, err.offline);
      return;
    }
    if (seq !== navSeq) return;

    const mine = spaces.filter((space) => space.kind === 'personal' && space.writable);
    const shared = spaces.filter((space) => space.kind === 'shared');
    const others = spaces.filter((space) => space.kind === 'personal' && !space.writable);
    const nodes = [h('div', { class: 'page-head' }, h('h1', null, '文件'), mine.length ? uploadActions() : null)];
    if (mine.length) {
      nodes.push(
        h('section', { class: 'space-group' }, h('h2', null, '我的'), h('div', { class: 'space-grid' }, mine.map(spaceCard)))
      );
    }
    if (shared.length) {
      nodes.push(
        h('section', { class: 'space-group' }, h('h2', null, '共享'), h('div', { class: 'space-grid' }, shared.map(spaceCard)))
      );
    }
    if (others.length) {
      nodes.push(
        h('section', { class: 'space-group' }, h('h2', null, '其他人的'), h('div', { class: 'space-grid' }, others.map(spaceCard)))
      );
    }
    if (!spaces.length) nodes.push(h('div', { class: 'empty' }, icon('drive'), '还没有可看的文件夹。'));
    main.replaceChildren(...nodes);
  }

  function spaceCard(space) {
    const own = space.kind === 'personal' && space.writable;
    const other = space.kind === 'personal' && !space.writable;
    const title = other ? space.name.replace(/ 的空间$/, '') + ' 上传的文件' : space.name;
    const note = space.writable ? '可以上传' : '只能看';
    return h(
      'a',
      { class: 'space-card' + (own ? ' mine' : ''), href: spaceHash(space.id, '') },
      icon('folder'),
      h('div', { class: 'body' }, h('div', { class: 'name' }, title), h('div', { class: 'note' }, note))
    );
  }

  // ---- 目录浏览 ----

  async function showBrowser(spaceId, path) {
    const seq = ++navSeq;
    showLoading();
    let listing;
    try {
      listing = await api('GET', '/api/fs/list?space=' + spaceId + '&path=' + encodeURIComponent(path));
    } catch (err) {
      if (seq !== navSeq || err.status === 401) return;
      state.listing = null;
      // 文件夹不存在时退回上一级；空间本身打不开时退回空间列表
      const parent = path.includes('/') ? spaceHash(spaceId, path.slice(0, path.lastIndexOf('/'))) : null;
      showProblem(err.message, err.offline ? null : path ? parent || spaceHash(spaceId, '') : '#/', err.offline);
      return;
    }
    if (seq !== navSeq) return;
    state.listing = listing;
    renderListing();
  }

  function breadcrumbs() {
    const { space, path } = state.listing;
    const segments = path ? path.split('/') : [];
    const items = [h('a', { href: '#/' }, '空间')];
    const push = (label, href, isLast) => {
      items.push(h('span', { class: 'sep' }, '/'));
      items.push(isLast ? h('span', { class: 'current' }, label) : h('a', { href }, label));
    };
    push(space.name, spaceHash(space.id, ''), segments.length === 0);
    segments.forEach((name, i) => {
      push(name, spaceHash(space.id, segments.slice(0, i + 1).join('/')), i === segments.length - 1);
    });
    return h('nav', { class: 'crumbs', 'aria-label': '当前位置' }, items);
  }

  function entryIcon(entry) {
    if (entry.type === 'dir') return 'folder';
    return { image: 'image', video: 'video', audio: 'audio' }[entry.kind] || 'file';
  }

  function tile(entry) {
    const { space, path } = state.listing;
    const thumb = h('div', { class: 'tile-thumb' });
    if (entry.thumb) {
      const img = h('img', { src: fileUrl('thumb', entry), alt: '', loading: 'lazy', decoding: 'async' });
      img.addEventListener('error', () => {
        img.replaceWith(icon(entryIcon(entry)));
        PC.checkConnection();
      });
      thumb.append(img);
    } else {
      thumb.append(icon(entryIcon(entry)));
    }
    if (entry.kind === 'video') thumb.append(h('span', { class: 'tile-play' }, icon('play')));

    const name = h('div', { class: 'tile-name' }, entry.name);
    const body =
      entry.type === 'dir'
        ? h(
            'a',
            {
              class: 'tile dir',
              href: spaceHash(space.id, path ? path + '/' + entry.name : entry.name),
              title: entry.name,
            },
            thumb,
            name,
            h('div', { class: 'tile-meta' }, '文件夹')
          )
        : h(
            'button',
            { class: 'tile', type: 'button', title: entry.name + '\n' + fmtTime(entry.mtime), onclick: () => openEntry(entry) },
            thumb,
            name,
            h('div', { class: 'tile-meta' }, fmtSize(entry.size))
          );
    return h('div', { class: 'tile-wrap' }, body, space.writable ? trashButton(entry) : null);
  }

  function trashButton(entry) {
    return h(
      'button',
      { class: 'tile-delete', type: 'button', title: '移到回收站', onclick: () => trashEntry(entry) },
      '删除'
    );
  }

  function renderListing() {
    const { space, entries } = state.listing;
    const head = h(
      'div',
      { class: 'page-head' },
      breadcrumbs(),
      space.writable ? uploadActions() : h('span', { class: 'badge' }, '只能看')
    );
    const nodes = [head];
    if (!space.writable) {
      nodes.push(h('p', { class: 'hint' }, '这里只能浏览和下载。'));
    }

    if (!entries.length) {
      nodes.push(h('div', { class: 'empty' }, icon('folder'), '这个文件夹是空的'));
      main.replaceChildren(...nodes);
      return;
    }

    const grid = h('div', { class: 'file-grid' });
    const foot = h('div', { class: 'list-foot' });
    nodes.push(grid, foot);
    main.replaceChildren(...nodes);

    let shown = 0;
    const seq = navSeq;
    const observer = new IntersectionObserver(
      (records) => {
        if (!records.some((r) => r.isIntersecting)) return;
        more();
        // 观察器只在"进入/离开视口"变化时回调。加载一批后页脚可能仍在视口内，
        // 重新观察一次以立刻再判断，否则高屏幕上会停在第一批不再加载。
        observer.unobserve(foot);
        if (shown < entries.length && seq === navSeq) observer.observe(foot);
      },
      { rootMargin: '800px' }
    );
    function more() {
      if (seq !== navSeq) return observer.disconnect();
      const next = entries.slice(shown, shown + BATCH);
      grid.append(...next.map(tile));
      shown += next.length;
      const folders = entries.filter((e) => e.type === 'dir').length;
      foot.textContent =
        shown < entries.length
          ? '已显示 ' + shown + ' / ' + entries.length + ' 项，继续向下滚动加载'
          : '共 ' + folders + ' 个文件夹，' + (entries.length - folders) + ' 个文件';
      if (shown >= entries.length) observer.disconnect();
    }
    more();
    observer.observe(foot);
  }

  function openEntry(entry) {
    if (entry.kind === 'image') openImageViewer(entry);
    else if (entry.kind === 'video' || entry.kind === 'audio') openPlayer(entry);
    else download(entry);
  }

  async function download(entry) {
    // 先确认电脑在线再发起下载，否则浏览器会跳到它自己的"无法访问"报错页
    if (!(await PC.checkConnection())) return;
    const link = h('a', { href: fileUrl('download', entry), download: entry.name });
    document.body.append(link);
    link.click();
    link.remove();
  }

  // ---- 预览 / 播放浮层 ----
  // 打开浮层时压入一条历史记录：手机上按"返回"是关闭预览，而不是离开当前文件夹。

  function openOverlay(node, onClose, onKey) {
    closeOverlay();
    const keyHandler = (event) => {
      if (event.key === 'Escape') closeOverlay();
      else if (onKey) onKey(event);
    };
    document.addEventListener('keydown', keyHandler);
    document.body.append(node);
    document.body.style.overflow = 'hidden';
    history.pushState({ pcOverlay: true }, '');
    overlay = { node, onClose, keyHandler };
  }

  function closeOverlay(fromHistory) {
    if (!overlay) return;
    const current = overlay;
    overlay = null;
    document.removeEventListener('keydown', current.keyHandler);
    if (current.onClose) current.onClose();
    current.node.remove();
    document.body.style.overflow = '';
    if (!fromHistory && history.state && history.state.pcOverlay) history.back();
  }

  function overlayFrame(stage) {
    const name = h('div', { class: 'name' });
    const meta = h('div', { class: 'meta' });
    const downloadBtn = h('button', { class: 'btn small', type: 'button', title: '下载' }, icon('download'), '下载');
    const deleteBtn = h('button', { class: 'btn small', type: 'button', title: '移到回收站' }, '删除');
    const node = h(
      'div',
      { class: 'overlay', role: 'dialog', 'aria-modal': 'true' },
      h(
        'div',
        { class: 'overlay-bar' },
        h('div', { class: 'overlay-title' }, name, meta),
        downloadBtn,
        state.listing && state.listing.space.writable ? deleteBtn : null,
        h('button', { class: 'btn small', type: 'button', title: '关闭', onclick: () => closeOverlay() }, icon('close'))
      ),
      stage
    );
    function describe(entry, extra) {
      name.textContent = entry.name;
      meta.textContent = [fmtSize(entry.size), fmtTime(entry.mtime), extra].filter(Boolean).join(' · ');
      downloadBtn.onclick = () => download(entry);
      deleteBtn.onclick = () => trashEntry(entry, true);
    }
    return { node, describe };
  }

  function openImageViewer(entry) {
    const images = state.listing.entries.filter((e) => e.kind === 'image');
    let index = images.indexOf(entry);
    const img = h('img', { alt: '' });
    const message = h('div', { class: 'overlay-message', hidden: true });
    const stage = h('div', { class: 'overlay-stage' }, img, message);
    const frame = overlayFrame(stage);

    function show(i) {
      index = (i + images.length) % images.length;
      const current = images[index];
      message.hidden = true;
      img.hidden = false;
      img.src = fileUrl('stream', current);
      frame.describe(current, index + 1 + ' / ' + images.length);
    }
    img.addEventListener('error', async () => {
      img.hidden = true;
      message.hidden = false;
      message.textContent = (await PC.checkConnection())
        ? '浏览器无法直接显示这张图片（例如 HEIC 格式），可以下载后查看。'
        : '连接已断开，恢复后请重新打开。';
    });

    if (images.length > 1) {
      stage.append(
        h('button', { class: 'btn overlay-nav prev', type: 'button', title: '上一张', onclick: () => show(index - 1) }, icon('prev')),
        h('button', { class: 'btn overlay-nav next', type: 'button', title: '下一张', onclick: () => show(index + 1) }, icon('next'))
      );
      // 左右滑动切换
      let startX = null;
      stage.addEventListener('touchstart', (e) => (startX = e.touches.length === 1 ? e.touches[0].clientX : null), { passive: true });
      stage.addEventListener('touchend', (e) => {
        if (startX === null) return;
        const dx = e.changedTouches[0].clientX - startX;
        if (Math.abs(dx) > 60) show(index + (dx < 0 ? 1 : -1));
        startX = null;
      });
    }
    openOverlay(frame.node, null, (event) => {
      if (images.length < 2) return;
      if (event.key === 'ArrowLeft') show(index - 1);
      else if (event.key === 'ArrowRight') show(index + 1);
    });
    show(index);
  }

  function openPlayer(entry) {
    const isVideo = entry.kind === 'video';
    const media = h(isVideo ? 'video' : 'audio', {
      src: fileUrl('stream', entry),
      controls: true,
      autoplay: true,
      playsinline: true,
      preload: 'metadata',
    });
    const message = h('div', { class: 'overlay-message', hidden: true });
    const frame = overlayFrame(h('div', { class: 'overlay-stage' }, media, message));
    frame.describe(entry);
    media.addEventListener('error', async () => {
      media.hidden = true;
      message.hidden = false;
      message.textContent = (await PC.checkConnection())
        ? '浏览器无法播放这个文件（视频不转码，部分编码不受支持），可以下载后用播放器观看。'
        : '连接已断开，恢复后请重新打开。';
    });
    openOverlay(frame.node, () => {
      // 关闭时停止播放并断开连接，不让视频在后台继续下载
      media.pause();
      media.removeAttribute('src');
      media.load();
    });
  }

  // ---- 上传 / 删除 ----

  function uploadActions() {
    return h('span', { class: 'head-actions' }, filePicker(false), filePicker(true));
  }

  function filePicker(folder) {
    const input = h('input', { type: 'file', multiple: true, hidden: true });
    if (folder) {
      input.setAttribute('webkitdirectory', '');
      input.setAttribute('directory', '');
    }
    let pending = null;
    input.addEventListener('change', () => {
      const files = [...input.files];
      const target = pending;
      pending = null;
      input.value = ''; // 允许连续两次选择同一批文件
      if (target && files.length) startUpload(files, target);
    });
    return h(
      'span',
      null,
      input,
      h(
        'button',
        {
          class: 'btn' + (folder ? '' : ' primary'),
          type: 'button',
          onclick: async () => {
            if (state.uploading) {
              toast('已有文件正在上传，请等这一批完成', 'error');
              return;
            }
            let target;
            try {
              target = await chooseFolder();
            } catch (err) {
              if (err.status !== 401) toast(err.message, 'error');
              return;
            }
            if (!target) return;
            pending = target;
            input.click();
          },
        },
        icon(folder ? 'folder' : 'upload'),
        folder ? '上传文件夹' : '上传文件'
      )
    );
  }

  function chooseFolder() {
    const here = state.listing && state.listing.space;
    return api('GET', '/api/spaces').then((data) => {
      const folders = data.spaces
        .filter((space) => space.writable && space.upload_root)
        .sort((a, b) => a.name.localeCompare(b.name, 'zh'));
      const preset = here && here.writable && here.upload_root ? String(here.id) : '';
      return new Promise((resolve) => {
        const error = h('p', { class: 'form-error', role: 'alert' });
        const name = h('input', { type: 'text', maxlength: 50, placeholder: '例如：假期', autocomplete: 'off' });
        const fresh = h('input', { type: 'radio', name: 'dest', value: 'new' });
        const sharedKind = h('input', { type: 'radio', name: 'kind', value: 'shared', checked: true });
        const personalKind = h('input', { type: 'radio', name: 'kind', value: 'personal' });
        const kindInputs = [sharedKind, personalKind];
        function syncKind() {
          kindInputs.forEach((input) => {
            input.disabled = !fresh.checked;
          });
        }
        const options = folders.map((space) => {
          const radio = h('input', {
            type: 'radio',
            name: 'dest',
            value: String(space.id),
            checked: String(space.id) === (preset || String(folders[0].id)),
            onchange: syncKind,
          });
          const tag = space.kind === 'personal' ? '个人' : '共享';
          return h(
            'label',
            { class: 'check folder-choice' },
            radio,
            h('span', { class: 'grow' }, space.name),
            h('span', { class: 'badge' }, tag)
          );
        });
        if (!folders.length) fresh.checked = true;
        fresh.addEventListener('change', syncKind);
        name.addEventListener('input', () => {
          fresh.checked = true;
          syncKind();
        });
        syncKind();
        const dialog = h(
          'dialog',
          null,
          h('h2', null, '保存到哪个文件夹'),
          h('p', { class: 'muted small' }, '文件直接放进这个文件夹，不再套很多层目录。'),
          error,
          h('div', { class: 'check-list' }, options),
          h('label', { class: 'check' }, fresh, '新建文件夹'),
          name,
          h(
            'div',
            { class: 'kind-pick' },
            h('label', { class: 'check' }, sharedKind, '共享'),
            h('label', { class: 'check' }, personalKind, '个人')
          ),
          h(
            'div',
            { class: 'buttons' },
            h('button', { class: 'btn', type: 'button', onclick: () => dialog.close() }, '取消'),
            h('button', { class: 'btn primary', type: 'button', onclick: () => confirm() }, '继续')
          )
        );
        let settled = false;
        function finish(value) {
          if (settled) return;
          settled = true;
          dialog.remove();
          resolve(value);
        }
        async function confirm() {
          error.textContent = '';
          try {
            if (fresh.checked || !folders.length) {
              const title = name.value.trim();
              if (!title) {
                error.textContent = '填一个文件夹名称';
                return;
              }
              const kind = personalKind.checked ? 'personal' : 'shared';
              const created = await api('POST', '/api/folders', { name: title, kind });
              finish({ space: created.id, path: '', label: created.name });
              return;
            }
            const selected = dialog.querySelector('input[name="dest"]:checked');
            const folder = folders.find((space) => String(space.id) === selected.value);
            finish({ space: folder.id, path: '', label: folder.name });
          } catch (err) {
            if (err.status === 401) finish(null);
            else error.textContent = err.message;
          }
        }
        dialog.addEventListener('close', () => finish(null));
        document.body.append(dialog);
        dialog.showModal();
        if (!folders.length) name.focus();
      });
    });
  }

  function relativeDir(file) {
    const rel = file.webkitRelativePath || '';
    const cut = Math.max(rel.lastIndexOf('/'), rel.lastIndexOf('\\'));
    return cut > 0 ? rel.slice(0, cut).replace(/\\/g, '/') : '';
  }

  function joinPath(base, extra) {
    if (!base) return extra || '';
    if (!extra) return base;
    return base + '/' + extra;
  }

  function startUpload(files, target) {
    if (state.uploading) {
      toast('已有文件正在上传，请等这一批完成', 'error');
      return;
    }
    const baseQuery = new URLSearchParams();
    baseQuery.set('space', String(target.space));

    document.querySelectorAll('.upload-panel').forEach((el) => el.remove());
    const name = h('div', { class: 'name' });
    const status = h('div', { class: 'status' }, '准备上传…');
    const bar = h('div');
    const progress = h('div', { class: 'progress' }, bar);
    const action = h('button', { class: 'btn small', type: 'button' }, '取消');
    const panel = h(
      'div',
      { class: 'upload-panel', role: 'status' },
      h('div', { class: 'row' }, name, action),
      progress,
      status,
      h('p', { class: 'hint' }, UPLOAD_HINT)
    );
    document.body.append(panel);

    let xhr = null;
    let cancelled = false;
    state.uploading = true;
    action.onclick = () => {
      cancelled = true;
      if (xhr) xhr.abort();
    };

    function finish(kind, text, extra) {
      state.uploading = false;
      xhr = null;
      panel.classList.add(kind);
      progress.hidden = true;
      status.replaceChildren(text, extra || '');
      action.textContent = '关闭';
      action.onclick = () => panel.remove();
    }

    function sendOne(file, index) {
      const query = new URLSearchParams(baseQuery);
      const dir = joinPath(target.path, relativeDir(file));
      if (dir) query.set('path', dir);
      if (file.lastModified > 0) query.set('mtime', String(Math.floor(file.lastModified)));
      name.textContent = files.length > 1 ? index + 1 + '/' + files.length + ' ' + file.name : file.name;
      name.title = file.name;
      return new Promise((resolve) => {
        xhr = new XMLHttpRequest();
        xhr.upload.addEventListener('progress', (event) => {
          if (!event.lengthComputable) return;
          const ratio = event.total ? event.loaded / event.total : 1;
          bar.style.width = (ratio * 100).toFixed(1) + '%';
          status.textContent =
            ratio >= 1 ? '已发送完毕，等待电脑保存…' : fmtSize(event.loaded) + ' / ' + fmtSize(event.total);
        });
        xhr.addEventListener('load', () => {
          let data = null;
          try {
            data = JSON.parse(xhr.responseText);
          } catch (err) {
            data = null;
          }
          if (xhr.status === 401) showLogin('登录已过期，请重新登录');
          resolve(xhr.status === 200 && data ? data : { error: PC.errorMessage(data, xhr.status) });
        });
        xhr.addEventListener('error', () => {
          PC.checkConnection();
          resolve({ error: '与电脑的连接断开了' });
        });
        xhr.addEventListener('abort', () => resolve({ error: '已取消', cancelled: true }));
        xhr.open('POST', '/api/web/upload' + (query.toString() ? '?' + query : ''));
        const form = new FormData();
        form.append('file', file, file.name);
        xhr.send(form);
      });
    }

    (async () => {
      let info;
      try {
        info = await api('GET', '/api/web/upload/info' + (baseQuery.toString() ? '?' + baseQuery : ''));
      } catch (err) {
        if (err.status !== 401) finish('failed', err.message);
        else finish('failed', '登录已过期，请重新登录');
        return;
      }
      if (cancelled) {
        finish('failed', '已取消上传。');
        return;
      }
      const tooBig = files.find((file) => file.size > info.max_bytes);
      if (tooBig) {
        finish('failed', tooBig.name + ' 超过单文件上限 ' + fmtSize(info.max_bytes));
        return;
      }
      const total = files.reduce((sum, file) => sum + file.size, 0);
      if (total > info.free_bytes) {
        finish('failed', '电脑磁盘剩余空间不足，无法上传这批文件');
        return;
      }

      let saved = 0;
      let last = null;
      let failure = '';
      for (let i = 0; i < files.length; i++) {
        if (cancelled) {
          failure = '已取消';
          break;
        }
        bar.style.width = '0%';
        const result = await sendOne(files[i], i);
        if (result && result.cancelled) {
          failure = '已取消';
          break;
        }
        if (!result || result.error) {
          failure = (files[i].name || '文件') + '：' + ((result && result.error) || '上传失败');
          break;
        }
        saved += 1;
        last = result;
      }
      const here = state.listing;
      if (saved && here && last && here.space.id === last.space) showBrowser(here.space.id, here.path);
      if (!failure && last) {
        const where = target.label + (last.dir ? '/' + last.dir : '');
        const text = saved > 1 ? '已上传 ' + saved + ' 个文件到 ' + where : '上传成功，已保存到 ' + where;
        const link = h('a', { href: spaceHash(last.space, last.dir), onclick: () => panel.remove() }, ' 查看');
        finish('success', text, link);
      } else {
        finish('failed', (saved ? '已上传 ' + saved + ' 个，随后中断。' : '') + (failure || '上传失败'));
      }
    })();
  }

  async function trashEntry(entry, fromOverlay) {
    const here = state.listing;
    if (!here || !here.space.writable) return;
    const full = here.path ? here.path + '/' + entry.name : entry.name;
    const what = entry.type === 'dir' ? '文件夹' : '文件';
    if (!confirm('把' + what + '“' + entry.name + '”移到电脑的回收站？')) return;
    try {
      await api('POST', '/api/fs/trash', { space: here.space.id, path: full });
    } catch (err) {
      if (err.status !== 401) toast(err.message, 'error');
      return;
    }
    if (fromOverlay) closeOverlay();
    toast('已移到回收站', 'ok');
    showBrowser(here.space.id, here.path);
  }

  window.addEventListener('beforeunload', (event) => {
    if (state.uploading) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  // ---- 事件 ----

  window.addEventListener('hashchange', () => {
    closeOverlay(true);
    route();
  });
  window.addEventListener('popstate', () => closeOverlay(true));

  PC.onUnauthorized = () => showLogin('登录已过期，请重新登录');
  PC.onReconnect = () => {
    // 连接恢复：未登录状态重新走启动流程，已登录则刷新当前页面内容
    if (!state.user) {
      if (!main.querySelector('form')) boot();
    } else if (!overlay) {
      route();
    }
  };

  boot();
})();
