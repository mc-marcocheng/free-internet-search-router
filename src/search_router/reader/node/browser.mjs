import { existsSync } from 'node:fs';

async function launchBrowser(options) {
  const { binaryInfo, launch } = await import('cloakbrowser');
  const info = binaryInfo();
  const binary = process.env.CLOAKBROWSER_BINARY_PATH || info.binaryPath;
  if (!existsSync(binary)) throw new Error('browser_not_installed');
  process.env.CLOAKBROWSER_BINARY_PATH = binary;
  return launch(options);
}

export async function acquireHtml(input, launch = launchBrowser) {
  let browser;
  let context;
  try {
    browser = await launch({
      headless: true,
      humanize: false,
      proxy: { server: input.proxy, bypass: '<-loopback>' },
      args: [
        '--proxy-bypass-list=<-loopback>', '--disable-quic',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        '--disable-background-networking', '--disable-component-update',
      ],
      launchOptions: { timeout: input.timeout, chromiumSandbox: process.platform === 'linux' },
    });
    context = await browser.newContext({
      serviceWorkers: 'block', acceptDownloads: false, ignoreHTTPSErrors: false,
    });
    await context.addInitScript(() => {
      delete globalThis.RTCPeerConnection;
      delete globalThis.webkitRTCPeerConnection;
    });
    await context.routeWebSocket('**/*', socket => socket.close());
    let requests = 0;
    let navigations = 0;
    let denied = false;
    const page = await context.newPage();
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password
          || !['GET', 'HEAD'].includes(request.method())
          || ['image', 'media', 'font'].includes(request.resourceType())) {
        return route.abort();
      }
      requests += 1;
      if (request.isNavigationRequest() && request.frame() === page.mainFrame()) navigations += 1;
      if (requests > 64 || navigations > 6) {
        denied = true;
        return route.abort();
      }
      return route.continue();
    });
    const response = await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: input.timeout });
    if (!response) throw new Error('browser_navigation_failed');
    if (response.status() >= 300) return { error: `document_http_${response.status()}` };
    await page.waitForLoadState('load', { timeout: Math.min(1500, input.timeout) }).catch(() => {});
    if (denied) return { error: 'browser_request_limit' };
    const html = await page.content();
    if (Buffer.byteLength(html, 'utf8') > input.limit) return { error: 'download_limit' };
    return {
      html, final_url: page.url(), content_type: 'text/html',
    };
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  }
}