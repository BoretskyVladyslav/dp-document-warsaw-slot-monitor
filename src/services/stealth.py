from playwright.async_api import BrowserContext, Page
from playwright_stealth import Stealth

_STEALTH = Stealth(
    navigator_languages_override=("uk-UA", "uk", "en-US", "en"),
    navigator_platform_override="Win32",
)


async def stealth_async(page: Page | BrowserContext) -> None:
    await _STEALTH.apply_stealth_async(page)


STEALTH_INIT_SCRIPT = """
(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  Object.defineProperty(navigator, 'languages', { get: () => ['uk-UA', 'uk', 'en-US', 'en', 'pl'] });
  Object.defineProperty(navigator, 'language', { get: () => 'uk-UA' });
  Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
  Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const plugins = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
        { name: 'Native Client', filename: 'internal-nacl-plugin' },
      ];
      plugins.item = (index) => plugins[index];
      plugins.namedItem = (name) => plugins.find((plugin) => plugin.name === name);
      plugins.refresh = () => undefined;
      return plugins;
    },
  });

  window.chrome = window.chrome || { runtime: {} };

  const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
    );
  }

  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (parameter) {
    if (parameter === 37445) return 'Google Inc. (Intel)';
    if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)';
    return getParameter.call(this, parameter);
  };

  Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
  Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
})();
"""

CLOUDFLARE_CLEARED_JS = """
() => {
  if (document.readyState === 'loading') return false;
  const title = (document.title || '').toLowerCase();
  if (['just a moment', 'checking your browser', 'attention required'].some((m) => title.includes(m))) {
    return false;
  }
  const overlay = document.querySelector('#challenge-running, #cf-spinner, .cf-browser-verification');
  if (overlay) return false;
  return true;
}
"""

CHROME_CLIENT_HINTS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7,pl;q=0.6",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Google Chrome";v="151", "Chromium";v="151", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
