import { htmlToMarkdown } from 'mdream';
import { acquireHtml } from './browser.mjs';

const chunks = [];
let size = 0;
let input;
try {
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 125000000) throw new Error('input_limit');
    chunks.push(chunk);
  }
  input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (input.mode === 'browser') {
    process.stdout.write(JSON.stringify(await acquireHtml(input)));
  } else {
    if (typeof input.html !== 'string' || typeof input.origin !== 'string') {
      throw new Error('invalid_input');
    }
    if (input.minimal !== undefined && typeof input.minimal !== 'boolean') {
      throw new Error('invalid_input');
    }
    const minimal = input.minimal ?? true;
    let text = htmlToMarkdown(input.html, { origin: input.origin, minimal });
    const fallback = minimal && !text.trim();
    if (fallback) text = htmlToMarkdown(input.html, { origin: input.origin });
    process.stdout.write(JSON.stringify({ text, fallback }));
  }
} catch (error) {
  if (input?.mode === 'browser') {
    process.stdout.write(JSON.stringify({ error: error.message === 'browser_not_installed'
      ? 'browser_not_installed' : 'browser_navigation_failed' }));
  } else {
    process.exitCode = 1;
  }
}