// Single source of truth for every root-relative path the app builds.
// The release build is served from '/', the static replay build from
// '/CrackRAG/demo/'; nothing else may hardcode a leading slash.
const base = import.meta.env.BASE_URL;

export function assetUrl(path: string) {
  return base + path.replace(/^\/+/, '');
}

export function apiUrl(path: string) {
  return assetUrl('api/v1' + path);
}

export function healthUrl() {
  return assetUrl('healthz');
}
