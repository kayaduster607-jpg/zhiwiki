/* 原生 Node 测试 GPU 资源生命周期；真实着色器与画面另外由浏览器验收。 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function fakeGL(options = {}) {
  const resources = new Set();
  const uploads = [];
  const draws = [];
  const matrices = new Map();
  const floats = new Map();
  let shaderCount = 0;
  let lost = false;

  function allocate(kind) {
    const resource = { kind };
    resources.add(resource);
    return resource;
  }

  function release(resource) {
    assert.equal(resources.delete(resource), true, "GPU 资源只能释放一次");
  }

  const gl = {
    resources, uploads, draws, matrices, floats,
    ARRAY_BUFFER: 34962, STATIC_DRAW: 35044, FLOAT: 5126,
    POINTS: 0, LINES: 1, TRIANGLES: 4, COLOR_BUFFER_BIT: 16384,
    DEPTH_TEST: 2929, CULL_FACE: 2884, BLEND: 3042,
    SRC_ALPHA: 770, ONE: 1, ONE_MINUS_SRC_ALPHA: 771,
    VERTEX_SHADER: 35633, FRAGMENT_SHADER: 35632,
    COMPILE_STATUS: 35713, LINK_STATUS: 35714,
    createShader() {
      shaderCount += 1;
      const shader = allocate("shader");
      shader.fail = options.failShader === shaderCount;
      return shader;
    },
    shaderSource(shader, source) { shader.source = source; },
    compileShader() {},
    getShaderParameter(shader) { return !shader.fail; },
    getShaderInfoLog() { return "synthetic compiler failure"; },
    createProgram() { return allocate("program"); },
    attachShader() {}, detachShader() {}, linkProgram() {},
    getProgramParameter() { return !options.failLink; },
    getProgramInfoLog() { return "synthetic linker failure"; },
    getUniformLocation(program, name) { return { program, name }; },
    getAttribLocation(program, name) { return name === "a_position" ? 0 : 1; },
    createBuffer() { return allocate("buffer"); },
    bindBuffer() {},
    bufferData(target, values) { uploads.push(Array.from(values)); },
    deleteShader: release, deleteProgram: release, deleteBuffer: release,
    uniformMatrix4fv(location, transpose, value) { matrices.set(location.name, Array.from(value)); },
    uniform1f(location, value) { floats.set(location.name, value); },
    enableVertexAttribArray() {}, vertexAttribPointer() {}, disableVertexAttribArray() {},
    drawArrays(type, first, count) { draws.push({ type, first, count }); },
    useProgram() {}, viewport() {}, clearColor() {}, clear() {},
    disable() {}, enable() {}, blendFuncSeparate() {},
    isContextLost() { return lost; },
    loseContext() { lost = true; }
  };
  return gl;
}

function setup(options = {}) {
  const gl = fakeGL(options);
  const window = { devicePixelRatio: options.pixelRatio || 1 };
  const context = vm.createContext({ window });
  const source = fs.readFileSync(path.join(__dirname, "../zhiwiki/web/orb-renderer.js"), "utf8");
  vm.runInContext(source, context);
  const canvas = { width: 0, height: 0, getContext: () => gl };
  return { api: window.ZhiOrbRenderer, canvas, gl };
}

function draw(renderer, frame = {}) {
  renderer.draw({ width: 800, height: 600, time: 2, yaw: 0.3, pitch: -0.2, ...frame });
}

test("orb uploads finite geometry within the shared world boundary", () => {
  const { api, canvas, gl } = setup();
  const renderer = api.create(canvas);
  assert.equal(gl.uploads.length, 3);
  gl.uploads.forEach((values) => assert.ok(values.every(Number.isFinite)));
  const positions = gl.uploads[0];
  assert.equal(positions.length / 7, 2834);
  for (let index = 0; index < positions.length; index += 7) {
    assert.ok(Math.hypot(...positions.slice(index, index + 3)) <= 1.65001);
    if (positions[index + 6] === 0) {
      const radius = Math.hypot(...positions.slice(index, index + 3));
      assert.ok(radius >= 1.14999 && radius <= 1.18001);
    }
  }
  draw(renderer);
  assert.deepEqual(gl.draws.map((item) => item.type), [gl.TRIANGLES, gl.LINES, gl.POINTS]);
  assert.equal(gl.draws[2].count, positions.length / 7);
  renderer.dispose();
});

test("orb respects DPR and total drawing-buffer pixel limits", () => {
  const { api, canvas, gl } = setup({ pixelRatio: 4 });
  const renderer = api.create(canvas);
  draw(renderer, { width: 400, height: 300 });
  assert.equal(canvas.width, 600);
  assert.equal(canvas.height, 450);
  draw(renderer, { width: 3000, height: 2000 });
  assert.ok(canvas.width * canvas.height <= 1500000);
  assert.ok(gl.floats.get("u_pixelRatio") <= 1.5);
  draw(renderer, { width: 1e8, height: 0.1 });
  assert.ok(canvas.width >= 1 && canvas.height >= 1);
  assert.ok(canvas.width * canvas.height <= 1500000);
  renderer.dispose();
});

test("orb uses the agreed perspective and world rotation for overlay alignment", () => {
  const { api, canvas, gl } = setup();
  const renderer = api.create(canvas);
  draw(renderer, { width: 1200, height: 600, yaw: Math.PI / 2, pitch: 0 });
  const projection = gl.matrices.get("u_projection");
  assert.ok(Math.abs(projection[5] - 1 / Math.tan(Math.PI / 8)) < 1e-6);
  assert.ok(Math.abs(projection[0] * 2 - projection[5]) < 1e-6);
  const rotation = gl.matrices.get("u_rotation");
  // yaw=90° 时世界 +X 指向远离相机的 -Z，避免 HTML 节点出现镜像漂移。
  assert.ok(Math.abs(rotation[0]) < 1e-6);
  assert.ok(Math.abs(rotation[2] + 1) < 1e-6);
  draw(renderer, { yaw: 0, pitch: Math.PI / 2 });
  assert.ok(Math.abs(gl.matrices.get("u_rotation")[6] - 1) < 1e-6);
  renderer.dispose();
});

test("orb dispose releases every resource once and stops rendering", () => {
  const { api, canvas, gl } = setup();
  const renderer = api.create(canvas);
  assert.equal(gl.resources.size, 6, "仅保留三个程序与三个缓冲区");
  assert.equal([...gl.resources].filter((item) => item.kind === "shader").length, 0);
  renderer.dispose();
  renderer.dispose();
  draw(renderer);
  assert.equal(gl.resources.size, 0);
  assert.equal(gl.draws.length, 0);
});

test("orb cleans partial resources when shader compilation or linking fails", () => {
  for (const options of [{ failShader: 1 }, { failShader: 4 }, { failLink: true }]) {
    const { api, canvas, gl } = setup(options);
    assert.throws(() => api.create(canvas), /着色器.*失败/);
    assert.equal(gl.resources.size, 0);
  }
});

test("orb context loss is reported for the owner's fallback and recreation", () => {
  const { api, canvas, gl } = setup();
  const renderer = api.create(canvas);
  gl.loseContext();
  assert.throws(() => draw(renderer), /上下文已丢失/);
  assert.equal(gl.draws.length, 0);
  renderer.dispose();
  assert.equal(gl.resources.size, 0);
});

test("orb ignores collapsed surfaces and sanitizes non-finite animation state", () => {
  const { api, canvas, gl } = setup();
  const renderer = api.create(canvas);
  draw(renderer, { width: 0 });
  draw(renderer, { height: NaN });
  assert.equal(gl.draws.length, 0);
  draw(renderer, { time: Infinity, yaw: NaN, pitch: Infinity });
  assert.ok(gl.matrices.get("u_rotation").every(Number.isFinite));
  assert.equal(gl.floats.get("u_time"), 0);
  renderer.dispose();
});

test("orb reports missing canvas and unavailable GPU capability clearly", () => {
  const { api } = setup();
  assert.throws(() => api.create(null), /canvas/);
  assert.throws(() => api.create({ getContext: () => null }), /WebGL1/);
});
