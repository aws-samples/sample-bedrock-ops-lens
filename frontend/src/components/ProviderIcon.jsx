// Provider icon — small <img> with a CSS invert filter applied in dark
// mode for icons whose dark/black elements are otherwise invisible on
// a dark background. Icons are bundled at build time from
// src/assets/provider-icons/ so they ship inside the SPA tarball — no
// runtime CDN dependency.

// Vite's import-with-glob lets us reference all icons by filename at
// build time without listing each one. The result is a path string the
// browser can fetch (resolved by Vite to a hashed asset URL).
const ICON_URLS = import.meta.glob(
  '../assets/provider-icons/*.png',
  { eager: true, import: 'default' }
);

// modelId provider segment → icon filename. Some Bedrock providers don't
// match the icon basename (e.g. anthropic uses claude.png), so this is a
// hand-curated map. Keep it short — fall through to null if missing.
const PROVIDER_ICON_FILE = {
  anthropic:  'claude.png',
  amazon:     'amazon.png',
  meta:       'meta.png',
  cohere:     'cohere.png',
  mistral:    'mistral.png',
  ai21:       'a21.png',
  deepseek:   'deepseek.png',
  qwen:       'qwen.png',
  twelvelabs: 'twelvelabs.png',
  writer:     'palmyra.png',
  nvidia:     'nvidia.png',
  google:     'gemma.png',
  moonshotai: 'kimi.png',
  // GPT-OSS ships under modelId prefix `openai.` (e.g. openai.gpt-oss-120b-1:0).
  // The internal version's spec called this `forge` but Bedrock actually
  // returns `openai`. Map both for safety.
  openai:     'gpt-oss.png',
  forge:      'gpt-oss.png',
  // Stability AI image / video models (modelId prefix `stability.`)
  stability:  'stabilityai.png',
};

// Icons whose visible elements are ~black-on-transparent. They become
// invisible on the dashboard's dark background, so we apply an invert
// filter only when the dark theme is active.
const NEEDS_INVERT = new Set(['amazon', 'openai', 'forge', 'moonshotai']);

function urlFor(filename) {
  const key = `../assets/provider-icons/${filename}`;
  return ICON_URLS[key] || null;
}

export default function ProviderIcon({ provider, size = 20, alt }) {
  const file = PROVIDER_ICON_FILE[provider];
  const src = file ? urlFor(file) : null;
  if (!src) {
    // Fallback: a small grey square so layouts don't shift when we
    // hit a provider we don't have an icon for.
    return (
      <span
        aria-label={alt || provider}
        style={{
          display: 'inline-block', width: size, height: size,
          background: '#5f6b7a', borderRadius: 3, verticalAlign: 'middle',
        }}
      />
    );
  }
  return (
    <img
      src={src}
      alt={alt || provider}
      style={{ width: size, height: size, objectFit: 'contain', verticalAlign: 'middle' }}
      className={NEEDS_INVERT.has(provider) ? 'provider-icon-invertable' : ''}
    />
  );
}
