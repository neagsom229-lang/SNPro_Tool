/**
 * hero.js — Professional 3D media pipeline hero scene using Three.js.
 * Features: Procedural PMREM environment map, smooth icosahedron core, 5 orbiting pipeline objects,
 * InstancedMesh waveform, drifting particles, delta-time physics, pointer parallax,
 * hover raycasting, theme switching, adaptive quality with JS+GPU frame cost, and ?perf=1 overlay.
 */

import {
  Scene,
  PerspectiveCamera,
  WebGLRenderer,
  PMREMGenerator,
  Clock,
  Vector2,
  Vector3,
  Raycaster,
  Color,
  FogExp2,
  AmbientLight,
  PointLight,
  DirectionalLight,
  IcosahedronGeometry,
  MeshStandardMaterial,
  Mesh,
  Group,
  BoxGeometry,
  InstancedMesh,
  Object3D,
  ConeGeometry,
  BufferGeometry,
  BufferAttribute,
  PointsMaterial,
  Points,
  LineBasicMaterial,
  Line,
  AdditiveBlending,
  BackSide,
  SRGBColorSpace
} from 'three';

export class Hero3DScene {
  constructor(container) {
    this.container = container;
    this.scene = null;
    this.camera = null;
    this.renderer = null;
    this.pmremGenerator = null;
    this.clock = new Clock();
    this.animationFrameId = null;
    this.isActive = false;

    // Interaction & Animation state
    this.mouseX = 0;
    this.mouseY = 0;
    this.targetMouseX = 0;
    this.targetMouseY = 0;
    this.scrollProgress = 0;
    this.introElapsed = 0;
    this.introDuration = 1.2;

    // Objects & Groups
    this.orbitObjects = [];
    this.orbitGroup = null;
    this.raycaster = new Raycaster();
    this.mouseVector = new Vector2(-999, -999);
    this.hoveredObject = null;
    this.particles = null;
    this.coreMesh = null;
    this.pointLight = null;
    this.rimLight = null;
    this.ambientLight = null;

    // Performance & Debug (?perf=1)
    this.showPerfOverlay = new URLSearchParams(window.location.search).get('perf') === '1';
    this.perfElement = null;
    this.frameTimes = [];
    this.jsFrameTimes = [];
    this.currentDPR = 1.5;
    this.particleCount = 400;
    this.dprReductionReason = 'optimal';
    this.lastFrameTimestamp = performance.now();
    this.fpsCap = 60;
    this.frameInterval = 1000 / this.fpsCap;
    this.accumulatedTime = 0;
    this.isReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.isLowPower = (navigator.hardwareConcurrency && navigator.hardwareConcurrency < 4) ||
                     (navigator.deviceMemory && navigator.deviceMemory < 2);

    // Bindings
    this.onMouseMove = this.onMouseMove.bind(this);
    this.onWindowResize = this.onWindowResize.bind(this);
    this.onVisibilityChange = this.onVisibilityChange.bind(this);
    this.onScroll = this.onScroll.bind(this);
    this.observer = null;
    this.resizeObserver = null;
    this.themeObserver = null;
  }

  init() {
    if (!this.container) return;

    if (this.isReducedMotion) {
      this.renderStaticFallback();
      return;
    }

    // 1. Scene & Camera
    this.scene = new Scene();
    const width = this.container.clientWidth || 400;
    const height = this.container.clientHeight || 350;

    this.camera = new PerspectiveCamera(45, width / height, 0.1, 1000);
    this.camera.position.set(0, 0, 7.5);

    // 2. Renderer with adaptive DPR & low-power throttling
    const isMobile = window.innerWidth < 768;
    this.currentDPR = Math.min(window.devicePixelRatio || 1, isMobile ? 1.5 : 2.0);
    if (this.isLowPower) {
      this.currentDPR = 1.0;
      this.dprReductionReason = 'low-power hardware (concurrency/memory)';
    }

    this.renderer = new WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: 'high-performance'
    });
    this.renderer.setSize(width, height);
    this.renderer.setPixelRatio(this.currentDPR);
    this.renderer.outputColorSpace = SRGBColorSpace;
    this.container.appendChild(this.renderer.domElement);

    // 3. Procedural Environment Map (PMREM) for glossy reflections
    this.pmremGenerator = new PMREMGenerator(this.renderer);
    this.pmremGenerator.compileEquirectangularShader();
    const envScene = new Scene();
    envScene.background = new Color(0x10131c);
    const lightSphereGeo = new IcosahedronGeometry(10, 2);
    const lightSphereMat = new MeshStandardMaterial({ color: 0xe3a857, side: BackSide });
    envScene.add(new Mesh(lightSphereGeo, lightSphereMat));
    const envMapRT = this.pmremGenerator.fromScene(envScene);
    this.scene.environment = envMapRT.texture;

    // 4. Fog & Theme Colors
    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const fogColor = isDark ? 0x10131c : 0xf2ede3;
    this.scene.fog = new FogExp2(fogColor, 0.035);

    // 5. Lighting
    this.ambientLight = new AmbientLight(isDark ? 0xffffff : 0x222222, isDark ? 1.0 : 1.4);
    this.scene.add(this.ambientLight);

    const accentColorHex = this.getThemeAccentHex();
    this.pointLight = new PointLight(accentColorHex, 4.0, 30);
    this.pointLight.position.set(0, 0, 1);
    this.scene.add(this.pointLight);

    this.rimLight = new DirectionalLight(0x4fb3a9, 2.5);
    this.rimLight.position.set(-5, 5, -3);
    this.scene.add(this.rimLight);

    // 6. Build Content
    this.buildCore();
    this.buildOrbitingObjects();
    this.buildParticles();

    // 7. Perf Overlay (?perf=1)
    if (this.showPerfOverlay) {
      this.createPerfOverlay();
    }

    // 8. Event Listeners & Observers
    window.addEventListener('mousemove', this.onMouseMove);
    window.addEventListener('scroll', this.onScroll, { passive: true });
    document.addEventListener('visibilitychange', this.onVisibilityChange);

    this.resizeObserver = new ResizeObserver(() => this.onWindowResize());
    this.resizeObserver.observe(this.container);

    this.observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          this.resume();
        } else {
          this.pause();
        }
      });
    }, { threshold: 0.1 });
    this.observer.observe(this.container);

    this.themeObserver = new MutationObserver(() => {
      this.updateThemeColors();
    });
    this.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    this.isActive = true;
    this.clock.start();
    this.animate();
  }

  getThemeAccentHex() {
    const style = getComputedStyle(document.documentElement);
    const val = style.getPropertyValue('--accent').trim();
    if (val.startsWith('#')) return val;
    return document.documentElement.getAttribute('data-theme') === 'light' ? '#b9781f' : '#e3a857';
  }

  updateThemeColors() {
    if (!this.scene) return;
    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const accentHex = this.getThemeAccentHex();

    this.scene.fog.color.setHex(isDark ? 0x10131c : 0xf2ede3);
    this.ambientLight.color.setHex(isDark ? 0xffffff : 0x222222);
    this.ambientLight.intensity = isDark ? 1.0 : 1.4;
    if (this.pointLight) {
      this.pointLight.color.set(accentHex);
    }
    if (this.coreMesh && this.coreMesh.material) {
      this.coreMesh.material.color.set(accentHex);
      this.coreMesh.material.emissive.set(accentHex);
    }
  }

  buildCore() {
    const accentHex = this.getThemeAccentHex();
    // Step 3: Smooth core using IcosahedronGeometry detail 4
    const coreGeo = new IcosahedronGeometry(1.1, 4);
    const coreMat = new MeshStandardMaterial({
      color: accentHex,
      emissive: accentHex,
      emissiveIntensity: 0.4,
      roughness: 0.15,
      metalness: 0.9
    });
    this.coreMesh = new Mesh(coreGeo, coreMat);
    this.scene.add(this.coreMesh);
  }

  buildOrbitingObjects() {
    this.orbitGroup = new Group();
    this.scene.add(this.orbitGroup);

    const matAmber = new MeshStandardMaterial({
      color: 0xe3a857,
      roughness: 0.2,
      metalness: 0.85
    });

    const matTeal = new MeshStandardMaterial({
      color: 0x4fb3a9,
      roughness: 0.15,
      metalness: 0.7
    });

    const matSilver = new MeshStandardMaterial({
      color: 0xd8dde8,
      roughness: 0.1,
      metalness: 0.95
    });

    // 1. Film Strip
    const filmGroup = new Group();
    const boxGeo = new BoxGeometry(0.35, 0.45, 0.08);
    for (let i = 0; i < 4; i++) {
      const bMesh = new Mesh(boxGeo, matSilver);
      bMesh.position.x = (i - 1.5) * 0.42;
      filmGroup.add(bMesh);
    }

    // 2. Waveform (InstancedMesh)
    const barCount = 18;
    const barGeo = new BoxGeometry(0.08, 0.6, 0.08);
    const barInstanced = new InstancedMesh(barGeo, matTeal, barCount);
    const dummy = new Object3D();
    for (let i = 0; i < barCount; i++) {
      dummy.position.set((i - barCount / 2) * 0.12, 0, 0);
      dummy.updateMatrix();
      barInstanced.setMatrixAt(i, dummy.matrix);
    }
    barInstanced.instanceMatrix.needsUpdate = true;
    barInstanced.userData = { isWaveform: true, count: barCount };

    // 3. Document
    const docGroup = new Group();
    const planeGeo = new BoxGeometry(0.6, 0.8, 0.04);
    for (let i = 0; i < 3; i++) {
      const pMesh = new Mesh(planeGeo, matSilver);
      pMesh.position.set(i * 0.06, i * 0.06, -i * 0.05);
      docGroup.add(pMesh);
    }

    // 4. Image Frame
    const frameGeo = new BoxGeometry(0.7, 0.7, 0.1);
    const frameMesh = new Mesh(frameGeo, matAmber);

    // 5. Play Button
    const playGeo = new ConeGeometry(0.4, 0.7, 3);
    const playMesh = new Mesh(playGeo, matTeal);
    playMesh.rotation.z = -Math.PI / 2;

    const definitions = [
      { obj: filmGroup, radius: 2.7, speed: 0.4, tilt: 0.3 },
      { obj: barInstanced, radius: 3.2, speed: -0.35, tilt: -0.4 },
      { obj: docGroup, radius: 2.4, speed: 0.5, tilt: 0.2 },
      { obj: frameMesh, radius: 3.0, speed: -0.45, tilt: 0.5 },
      { obj: playMesh, radius: 2.2, speed: 0.6, tilt: -0.2 }
    ];

    definitions.forEach((def, index) => {
      const pivot = new Group();
      pivot.add(def.obj);
      def.obj.position.set(def.radius, 0, 0);
      pivot.rotation.x = def.tilt;
      pivot.rotation.z = def.tilt * 0.5;

      this.orbitGroup.add(pivot);
      this.orbitObjects.push({
        pivot,
        object: def.obj,
        speed: def.speed,
        radius: def.radius,
        targetScale: 1.0
      });

      const orbitLineGeo = new BufferGeometry();
      const points = [];
      const segs = 64;
      for (let s = 0; s <= segs; s++) {
        const theta = (s / segs) * Math.PI * 2;
        points.push(new Vector3(Math.cos(theta) * def.radius, 0, Math.sin(theta) * def.radius));
      }
      orbitLineGeo.setFromPoints(points);
      const lineMat = new LineBasicMaterial({
        color: 0xe3a857,
        transparent: true,
        opacity: 0.12
      });
      const orbitLine = new Line(orbitLineGeo, lineMat);
      orbitLine.rotation.x = def.tilt;
      orbitLine.rotation.z = def.tilt * 0.5;
      this.scene.add(orbitLine);
    });
  }

  buildParticles() {
    const count = this.particleCount;
    const geometry = new BufferGeometry();
    const positions = new Float32Array(count * 3);
    const scales = new Float32Array(count);

    for (let i = 0; i < count * 3; i += 3) {
      positions[i] = (Math.random() - 0.5) * 12;
      positions[i + 1] = (Math.random() - 0.5) * 12;
      positions[i + 2] = (Math.random() - 0.5) * 12;
      scales[i / 3] = Math.random();
    }

    geometry.setAttribute('position', new BufferAttribute(positions, 3));
    geometry.setAttribute('scale', new BufferAttribute(scales, 1));

    const material = new PointsMaterial({
      color: 0xe3a857,
      size: 0.05,
      transparent: true,
      opacity: 0.45,
      blending: AdditiveBlending
    });

    this.particles = new Points(geometry, material);
    this.scene.add(this.particles);
  }

  createPerfOverlay() {
    this.perfElement = document.createElement('div');
    this.perfElement.style.cssText = `
      position: absolute; top: 10px; left: 10px; z-index: 1000;
      background: rgba(16,19,28,0.9); color: #f8f9fa; font-family: monospace;
      font-size: 11px; padding: 8px 12px; border-radius: 6px; pointer-events: none;
      border: 1px solid rgba(227,168,87,0.35); backdrop-filter: blur(4px);
      line-height: 1.4; max-width: 260px;
    `;
    this.perfElement.textContent = 'Initializing 3D Perf...';
    this.container.style.position = 'relative';
    this.container.appendChild(this.perfElement);
  }

  onMouseMove(e) {
    if (!this.container) return;
    const rect = this.container.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    this.targetMouseX = (x / rect.width) * 2 - 1;
    this.targetMouseY = -(y / rect.height) * 2 + 1;

    this.mouseVector.x = this.targetMouseX;
    this.mouseVector.y = this.targetMouseY;
  }

  onWindowResize() {
    if (!this.container || !this.renderer || !this.camera) return;
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height);
  }

  onVisibilityChange() {
    if (document.hidden) {
      this.pause();
    } else {
      this.resume();
    }
  }

  onScroll() {
    const maxScroll = document.body.scrollHeight - window.innerHeight;
    this.scrollProgress = maxScroll > 0 ? window.scrollY / maxScroll : 0;
  }

  pause() {
    if (this.animationFrameId) {
      cancelAnimationFrame(this.animationFrameId);
      this.animationFrameId = null;
    }
  }

  resume() {
    if (this.isActive && !this.animationFrameId && !document.hidden && !this.isReducedMotion) {
      this.clock.start();
      this.animate();
    }
  }

  renderStaticFallback() {
    if (!this.container) return;
    this.container.innerHTML = `
      <div class="d-flex align-items-center justify-content-center h-100 rounded-4 p-4"
           style="background: var(--surface); border: 1px solid var(--border); min-height: 380px;">
        <div class="text-center">
          <div class="mb-3 text-accent fs-1"><i class="bi bi-box-seam"></i></div>
          <h5 class="display-font mb-1">SNPro 3D Hero Scene</h5>
          <p class="text-dim small mb-0">Static presentation (Reduced motion or WebGL fallback active)</p>
        </div>
      </div>
    `;
  }

  animate() {
    if (!this.isActive) return;

    const now = performance.now();
    const elapsedSinceLast = now - this.lastFrameTimestamp;

    // Cap rendering at ~60 FPS (or ~30 FPS for reduced motion / low-power)
    const targetInterval = (this.isReducedMotion || this.isLowPower) ? 33.33 : this.frameInterval;
    if (elapsedSinceLast < targetInterval) {
      this.animationFrameId = requestAnimationFrame(() => this.animate());
      return;
    }

    const dt = Math.min(this.clock.getDelta(), 0.1);
    const elapsed = this.clock.getElapsedTime();
    this.lastFrameTimestamp = now;

    const jsStart = performance.now();

    // 1. Intro Animation progress
    if (this.introElapsed < this.introDuration) {
      this.introElapsed += dt;
    }
    const introProgress = Math.min(1, this.introElapsed / this.introDuration);
    const easeOutCubic = 1 - Math.pow(1 - introProgress, 3);

    // 2. Pointer Parallax
    const damping = 1 - Math.exp(-6.0 * dt);
    this.mouseX += (this.targetMouseX - this.mouseX) * damping;
    this.mouseY += (this.targetMouseY - this.mouseY) * damping;

    this.camera.position.x = this.mouseX * 0.4;
    this.camera.position.y = this.mouseY * 0.4 + Math.sin(elapsed * 0.8) * 0.1;
    this.camera.position.z = 7.5 + this.scrollProgress * 2.0;
    this.camera.lookAt(0, 0, 0);

    // 3. Core Pulse & Rotation
    if (this.coreMesh) {
      this.coreMesh.rotation.x += 0.2 * dt;
      this.coreMesh.rotation.y += 0.3 * dt;
      const scaleVal = (1.0 + Math.sin(elapsed * 2.5) * 0.05) * easeOutCubic;
      this.coreMesh.scale.set(scaleVal, scaleVal, scaleVal);
    }

    // 4. Orbiting objects animation & raycasting hover
    this.raycaster.setFromCamera(this.mouseVector, this.camera);
    const intersectObjects = this.orbitObjects.map(o => o.object);
    const intersects = this.raycaster.intersectObjects(intersectObjects, true);
    this.hoveredObject = intersects.length > 0 ? intersects[0].object : null;

    this.orbitObjects.forEach(item => {
      item.pivot.rotation.y += item.speed * dt;

      const isHovered = this.hoveredObject && (this.hoveredObject === item.object || item.object.getObjectById(this.hoveredObject.id));
      item.targetScale = isHovered ? 1.2 : 1.0;
      const currentScale = item.object.scale.x;
      const newScale = currentScale + (item.targetScale * easeOutCubic - currentScale) * 0.1;
      item.object.scale.set(newScale, newScale, newScale);

      item.object.rotation.x += 0.5 * dt;
      item.object.rotation.y += 0.7 * dt;
    });

    // 5. Waveform animation
    this.orbitObjects.forEach(item => {
      if (item.object.userData && item.object.userData.isWaveform) {
        const instMesh = item.object;
        const count = instMesh.userData.count;
        const dummy = new Object3D();
        for (let i = 0; i < count; i++) {
          const waveHeight = 0.6 + Math.sin(elapsed * 4.0 + i * 0.4) * 0.3;
          dummy.position.set((i - count / 2) * 0.12, 0, 0);
          dummy.scale.set(1, waveHeight, 1);
          dummy.updateMatrix();
          instMesh.setMatrixAt(i, dummy.matrix);
        }
        instMesh.instanceMatrix.needsUpdate = true;
      }
    });

    // 6. Drifting particles
    if (this.particles) {
      this.particles.rotation.y += 0.05 * dt;
      this.particles.rotation.x += 0.02 * dt;
    }

    // 7. Render
    if (this.renderer && this.scene && this.camera) {
      this.renderer.render(this.scene, this.camera);
    }

    const jsTime = performance.now() - jsStart;
    this.jsFrameTimes.push(jsTime);
    this.frameTimes.push(dt * 1000);

    if (this.frameTimes.length > 60) {
      const avgFT = this.frameTimes.reduce((a, b) => a + b, 0) / this.frameTimes.length;
      const avgJS = this.jsFrameTimes.reduce((a, b) => a + b, 0) / this.jsFrameTimes.length;
      this.frameTimes.shift();
      this.jsFrameTimes.shift();

      // Adaptive quality: JS+GPU frame cost (> 22ms frame time drops DPR)
      if (avgFT > 22 && this.currentDPR > 1.0) {
        this.currentDPR = 1.0;
        this.renderer.setPixelRatio(1.0);
        this.dprReductionReason = `high frame time (${avgFT.toFixed(1)}ms > 22ms threshold)`;
      } else if (avgFT < 12 && this.currentDPR < 2.0 && window.innerWidth >= 768 && !this.isLowPower) {
        this.currentDPR = Math.min(window.devicePixelRatio || 1, 2.0);
        this.renderer.setPixelRatio(this.currentDPR);
        this.dprReductionReason = 'optimal performance (restored DPR)';
      }

      if (this.showPerfOverlay && this.perfElement && this.renderer) {
        const fps = Math.round(1000 / avgFT);
        const info = this.renderer.info;
        const renderCalls = info.render.calls;
        const triangles = info.render.triangles;
        const geometries = info.memory.geometries;
        const textures = info.memory.textures;

        this.perfElement.innerHTML = `
          <div><b>3D Perf Overlay</b></div>
          <div>FPS: ${fps} | FT: ${avgFT.toFixed(1)}ms</div>
          <div>JS Time: ${avgJS.toFixed(2)}ms</div>
          <div>Draw Calls: ${renderCalls}</div>
          <div>Triangles: ${triangles}</div>
          <div>Geom / Tex: ${geometries} / ${textures}</div>
          <div>DPR: ${this.currentDPR.toFixed(1)} (${this.dprReductionReason})</div>
          <div>Particles: ${this.particleCount}</div>
        `;
      }
    }

    this.animationFrameId = requestAnimationFrame(() => this.animate());
  }

  dispose() {
    this.isActive = false;
    this.pause();

    window.removeEventListener('mousemove', this.onMouseMove);
    window.removeEventListener('scroll', this.onScroll);
    document.removeEventListener('visibilitychange', this.onVisibilityChange);

    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this.observer) this.observer.disconnect();
    if (this.themeObserver) this.themeObserver.disconnect();

    if (this.perfElement && this.perfElement.parentNode) {
      this.perfElement.parentNode.removeChild(this.perfElement);
    }

    if (this.pmremGenerator) {
      this.pmremGenerator.dispose();
    }

    if (this.scene) {
      this.scene.traverse(child => {
        if (child.geometry) child.geometry.dispose();
        if (child.material) {
          if (Array.isArray(child.material)) {
            child.material.forEach(mat => mat.dispose());
          } else {
            child.material.dispose();
          }
        }
      });
    }

    if (this.renderer) {
      this.renderer.dispose();
      if (this.renderer.domElement && this.renderer.domElement.parentNode) {
        this.renderer.domElement.parentNode.removeChild(this.renderer.domElement);
      }
    }
  }
}
