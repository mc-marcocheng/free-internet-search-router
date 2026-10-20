import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { htmlToMarkdown } from 'mdream';
import { acquireHtml } from './browser.mjs';

test('bridge returns exactly mdream output', () => {
  const html = '<nav>Menu</nav><main><h1>Article</h1><p>Original <b>words</b>.</p></main><aside>Other articles</aside>';
  const origin = 'https://example.org/story';
  const expected = htmlToMarkdown(html, { origin, minimal: true });
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('./reader.mjs', import.meta.url))], {
    input: JSON.stringify({ html, origin }), encoding: 'utf8',
  });
  assert.equal(result.status, 0);
  assert.equal(JSON.parse(result.stdout).text, expected);
  assert.ok(expected.includes('Original'));
  assert.ok(!expected.includes('Other articles'));
});

test('bridge fails without leaking invalid input', () => {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('./reader.mjs', import.meta.url))], {
    input: 'private invalid input', encoding: 'utf8',
  });
  assert.notEqual(result.status, 0);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr, '');
});

test('bridge supports standard conversion without minimal filtering', () => {
  const html = '<main><p>Article body.</p></main><aside>Additional financial details.</aside>';
  const origin = 'https://example.org/filing';
  const expected = htmlToMarkdown(html, { origin });
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('./reader.mjs', import.meta.url))], {
    input: JSON.stringify({ html, origin, minimal: false }), encoding: 'utf8',
  });
  assert.equal(result.status, 0);
  const converted = JSON.parse(result.stdout);
  assert.equal(converted.text, expected);
  assert.equal(converted.fallback, false);
  assert.ok(converted.text.includes('Additional financial details'));
  assert.ok(!htmlToMarkdown(html, { origin, minimal: true }).includes('Additional financial details'));
});

test('bridge retains nonempty metadata-only output when minimal is requested', () => {
  const html = '<html><head><title>Financial results</title></head><body><p>Detailed operating results.</p></body></html>';
  const origin = 'https://example.org/filing';
  const expected = htmlToMarkdown(html, { origin, minimal: true });
  assert.ok(expected.trim());
  assert.ok(!expected.includes('Detailed operating results'));
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('./reader.mjs', import.meta.url))], {
    input: JSON.stringify({ html, origin, minimal: true }), encoding: 'utf8',
  });
  assert.equal(result.status, 0);
  assert.equal(JSON.parse(result.stdout).text, expected);
  assert.equal(JSON.parse(result.stdout).fallback, false);
});

test('bridge falls back to exact standard mdream output when minimal is blank', () => {
  const html = '<html><body><p>Financial statement</p><table><tr><td>Revenue</td><td>100</td></tr></table></body></html>';
  const origin = 'https://example.org/filing';
  assert.equal(htmlToMarkdown(html, { origin, minimal: true }).trim(), '');
  const expected = htmlToMarkdown(html, { origin });
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('./reader.mjs', import.meta.url))], {
    input: JSON.stringify({ html, origin }), encoding: 'utf8',
  });
  assert.equal(result.status, 0);
  const converted = JSON.parse(result.stdout);
  assert.equal(converted.text, expected);
  assert.equal(converted.fallback, true);
  assert.ok(converted.text.includes('Revenue'));
});

test('bridge keeps truly empty documents empty', () => {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('./reader.mjs', import.meta.url))], {
    input: JSON.stringify({ html: '<html><body></body></html>', origin: '' }), encoding: 'utf8',
  });
  assert.equal(result.status, 0);
  assert.equal(JSON.parse(result.stdout).text.trim(), '');
});

test('browser uses the proxy and closes resources on navigation failure', async () => {
  const closed = [];
  const context = {
    addInitScript: async () => {}, routeWebSocket: async () => {}, route: async () => {},
    newPage: async () => ({ goto: async () => { throw new Error('navigation failed'); } }),
    close: async () => closed.push('context'),
  };
  const launch = async options => {
    assert.equal(options.proxy.server, 'http://127.0.0.1:12345');
    assert.equal(options.proxy.bypass, '<-loopback>');
    assert.equal(options.humanize, false);
    return {
      newContext: async options => {
        assert.equal(options.serviceWorkers, 'block');
        return context;
      },
      close: async () => closed.push('browser'),
    };
  };
  await assert.rejects(acquireHtml({ url: 'https://example.org', proxy: 'http://127.0.0.1:12345', timeout: 1000 }, launch));
  assert.deepEqual(closed, ['context', 'browser']);
});