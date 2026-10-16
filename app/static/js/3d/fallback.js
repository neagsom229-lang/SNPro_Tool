/**
 * fallback.js — Graceful static SVG/CSS visual representation when WebGL is unavailable or disabled.
 */

export function setupFallback(container) {
  if (!container) return;
  container.innerHTML = `
    <div class="hero-3d-fallback d-flex align-items-center justify-content-center h-100 rounded-4 p-4"
         style="background: var(--surface); border: 1px dashed var(--border); min-height: 320px;">
      <div class="text-center">
        <div class="mb-3">
          <i class="bi bi-box-seam fs-1 text-accent" aria-hidden="true"></i>
        </div>
        <h5 class="display-font mb-1">SNPro Media Pipeline</h5>
        <p class="text-muted small mb-0">High-performance AI & media toolkit</p>
      </div>
    </div>
  `;
}
