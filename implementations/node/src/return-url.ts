import type { Config } from './config.js';
export function allowedReturn(config: Config, value: unknown): boolean {
  if (typeof value !== 'string' || /[\r\n\\]/.test(value)) return false;
  try {
    const url = new URL(value);
    if (
      decodeURIComponent(url.pathname)
        .split('/')
        .some((p) => p === '.' || p === '..')
    )
      return false;
    if (
      url.username ||
      url.password ||
      url.hash ||
      !['https:', 'http:', 'uber:'].includes(url.protocol)
    )
      return false;
    return config.returnUrls.some((entry) => {
      const rule = new URL(entry.trim());
      return (
        !rule.search &&
        !rule.hash &&
        url.protocol === rule.protocol &&
        url.host === rule.host &&
        (url.pathname === rule.pathname ||
          (rule.pathname.endsWith('/') && url.pathname.startsWith(rule.pathname)))
      );
    });
  } catch {
    return false;
  }
}
