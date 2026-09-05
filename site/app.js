const translated = [...document.querySelectorAll('[data-en]')];
for (const element of translated) element.dataset.zh = element.textContent;
const clients = {
  openclaw: ['OpenClaw', '01-openclaw-trusted-task.jpg', 1720, 1238],
  dsh: ['DSH', '02-dsh-trusted-task.jpg', 1720, 1238],
  codex: ['Codex', '03-codex-trusted-task.png', 1600, 337],
  hermes: ['Hermes Agent', '04-hermes-trusted-task.jpg', 1720, 900],
};
let language = new URLSearchParams(location.search).get('lang') === 'en' ? 'en' : 'zh';
let selectedClient = 'openclaw';
const tabs = [...document.querySelectorAll('[data-client]')];
function showClient(key) {
  selectedClient = key;
  const [name, file, width, height] = clients[key];
  const shot = document.querySelector('#client-shot');
  shot.src = `assets/${file}`;
  shot.width = width;
  shot.height = height;
  shot.alt = `${name} native application trusted-task execution screenshot`;
  document.querySelector('#full-shot').href = shot.src;
  document.querySelector('#client-caption').textContent = `${name} · ${language === 'en' ? 'Native client trusted-task run' : '原生应用可信任务实测'}`;
  document.querySelector('#client-panel').setAttribute('aria-labelledby', `tab-${key}`);
  for (const tab of tabs) {
    const active = tab.dataset.client === key;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  }
}
function setLanguage() {
  document.documentElement.lang = language === 'en' ? 'en' : 'zh-CN';
  document.title = language === 'en' ? 'CubeSandbox Agent Adapter — Controlled execution for local agents' : 'CubeSandbox Agent Adapter — 受控执行，自由协作';
  for (const element of translated) element.textContent = element.dataset[language];
  const toggle = document.querySelector('#language');
  toggle.textContent = language === 'en' ? '中文 ↗' : 'EN ↗';
  toggle.setAttribute('aria-label', language === 'en' ? '切换到中文' : 'Switch to English');
  const base = 'https://github.com/aik8s/cubesandbox-agent-adapter/blob/main/';
  document.querySelector('[data-doc=docker]').href = base + (language === 'en' ? 'docs/deploy-docker.md' : 'docs/deploy-docker.zh-CN.md');
  document.querySelector('[data-doc=kubernetes]').href = base + (language === 'en' ? 'README.md#quick-start-kubernetes-adapter' : 'README.zh-CN.md#一键部署-kubernetes-adapter');
  document.querySelector('#copy-status').textContent = '';
  showClient(selectedClient);
}
document.querySelector('#language').addEventListener('click', () => {
  language = language === 'en' ? 'zh' : 'en';
  const url = new URL(location.href);
  url.searchParams.set('lang', language);
  history.replaceState(null, '', url);
  setLanguage();
});
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener('click', () => showClient(tab.dataset.client));
  tab.addEventListener('keydown', event => {
    const offsets = {ArrowRight: 1, ArrowLeft: -1};
    if (!(event.key in offsets) && event.key !== 'Home' && event.key !== 'End') return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + offsets[event.key] + tabs.length) % tabs.length;
    tabs[next].focus();
    showClient(tabs[next].dataset.client);
  });
}
document.querySelector('#copy').addEventListener('click', async () => {
  const status = document.querySelector('#copy-status');
  try {
    await navigator.clipboard.writeText(document.querySelector('#pull-command').textContent);
    status.textContent = language === 'en' ? 'Copied.' : '已复制。';
  } catch {
    status.textContent = language === 'en' ? 'Please select and copy the command above.' : '请选中上方命令手动复制。';
  }
});
setLanguage();
