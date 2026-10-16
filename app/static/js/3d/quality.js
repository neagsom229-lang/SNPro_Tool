/**
 * quality.js — Hardware capability detection & progressive enhancement check for 3D scene.
 */

export function check3DCapability() {
  // 1. Check prefers-reduced-motion
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return { enabled: false, reason: 'reduced-motion preference' };
  }

  // 2. Check Data Saver mode
  if (navigator.connection && navigator.connection.saveData) {
    return { enabled: false, reason: 'data-saver active' };
  }

  // 3. Check low-end device memory (< 2 GB RAM) or CPU cores (< 4 threads)
  if (navigator.deviceMemory && navigator.deviceMemory < 2) {
    return { enabled: false, reason: 'low device memory (< 2GB)' };
  }
  if (navigator.hardwareConcurrency && navigator.hardwareConcurrency < 4) {
    return { enabled: false, reason: 'low core count (< 4 cores)' };
  }

  // 4. Check WebGL support
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) {
      return { enabled: false, reason: 'WebGL not supported' };
    }
  } catch (e) {
    return { enabled: false, reason: 'WebGL initialization error' };
  }

  return { enabled: true, reason: 'hardware capable' };
}
