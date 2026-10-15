/* 原生 WebGL1 知识球：程序化几何，不加载图片、字体或第三方渲染库。 */
(function (global) {
  "use strict";

  const MAX_PIXELS = 1500000;
  const TAU = Math.PI * 2;
  const STRIDE = 7 * Float32Array.BYTES_PER_ELEMENT;

  const VERTEX = `
    precision highp float;
    attribute vec3 a_position;
    attribute vec4 a_meta;
    uniform mat4 u_projection;
    uniform mat4 u_rotation;
    uniform float u_time;
    uniform float u_pixelRatio;
    varying mediump vec4 v_color;
    void main() {
      float shell = 1.0 - step(0.5, a_meta.w);
      float orbit = step(0.5, a_meta.w) * (1.0 - step(1.5, a_meta.w));
      float breath = 1.0 + shell * 0.009 * sin(u_time * 0.65 + a_meta.z);
      vec4 world = u_rotation * vec4(a_position * breath, 1.0);
      vec4 view = world - vec4(0.0, 0.0, 6.0, 0.0);
      gl_Position = u_projection * view;
      float pulse = 0.72 + 0.28 * sin(a_meta.z * 3.0 + u_time * 0.75);
      float trail = pow(max(0.0, cos(a_meta.z - u_time * 0.7)), 18.0);
      float intensity = mix(pulse, 0.23 + trail * 1.6, orbit);
      float front = smoothstep(-1.65, 1.65, world.z);
      vec3 mint = vec3(0.33, 0.97, 0.78);
      vec3 silver = vec3(0.88, 1.0, 0.96);
      v_color = vec4(mix(mint, silver, 0.12 + front * 0.6),
                     a_meta.y * intensity * (0.45 + front * 0.55));
      gl_PointSize = clamp(a_meta.x * u_pixelRatio * 6.0 / -view.z,
                           1.0, 18.0 * u_pixelRatio);
    }
  `;

  const POINT_FRAGMENT = `
    precision mediump float;
    varying mediump vec4 v_color;
    void main() {
      vec2 point = gl_PointCoord * 2.0 - 1.0;
      float radius = dot(point, point);
      if (radius > 1.0) discard;
      float glow = exp(-radius * 3.8);
      gl_FragColor = vec4(v_color.rgb, v_color.a * glow);
    }
  `;

  const LINE_FRAGMENT = `
    precision mediump float;
    varying mediump vec4 v_color;
    void main() { gl_FragColor = v_color; }
  `;

  const FACET_VERTEX = `
    precision highp float;
    attribute vec3 a_position;
    attribute vec3 a_normal;
    uniform mat4 u_projection;
    uniform mat4 u_rotation;
    varying mediump vec4 v_color;
    void main() {
      vec4 world = u_rotation * vec4(a_position, 1.0);
      vec3 normal = normalize(mat3(u_rotation) * a_normal);
      vec3 light = normalize(vec3(-0.45, 0.7, 1.0));
      float facing = abs(dot(normal, light));
      float glint = pow(max(0.0, dot(reflect(-light, normal), vec3(0.0, 0.0, 1.0))), 16.0);
      v_color = vec4(mix(vec3(0.17, 0.7, 0.54), vec3(0.89, 1.0, 0.97), facing),
                     0.018 + facing * 0.075 + glint * 0.22);
      gl_Position = u_projection * (world - vec4(0.0, 0.0, 6.0, 0.0));
    }
  `;

  // 固定种子让录屏、截图和低动态模式保持相同的空间结构。
  function randomGenerator(seed) {
    let value = seed >>> 0;
    return function () {
      value = (Math.imul(value, 1664525) + 1013904223) >>> 0;
      return value / 4294967296;
    };
  }

  function vertex(target, position, size, alpha, phase, kind) {
    target.push(position[0], position[1], position[2], size, alpha, phase, kind);
  }

  function spherePoints(target) {
    const random = randomGenerator(42871);
    const count = 2450;
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    for (let index = 0; index < count; index += 1) {
      const height = 1 - (2 * index + 1) / count;
      const latitude = Math.sqrt(1 - height * height);
      const angle = index * goldenAngle;
      const radius = 1.15 + random() * 0.03;
      const position = [Math.cos(angle) * latitude * radius, height * radius,
        Math.sin(angle) * latitude * radius];
      const bright = random() > 0.955;
      vertex(target, position, bright ? 4.2 : 1.7 + random() * 1.1,
        bright ? 1 : 0.56 + random() * 0.42, random() * TAU, 0);
    }
    // 内层稀疏星尘让外壳保留空心感，同时强调核心的纵深。
    for (let index = 0; index < 180; index += 1) {
      const angle = random() * TAU;
      const height = random() * 2 - 1;
      const radius = 0.4 + random() * 0.36;
      const latitude = Math.sqrt(1 - height * height);
      vertex(target, [Math.cos(angle) * latitude * radius, height * radius,
        Math.sin(angle) * latitude * radius], 1.4 + random() * 1.7, 0.455,
      angle, 2);
    }
  }

  function orbitPosition(angle, radius, tilt, turn) {
    const x = Math.cos(angle) * radius;
    const y = Math.sin(angle) * radius * 0.94;
    const tiltedY = y * Math.cos(tilt);
    const z = y * Math.sin(tilt);
    return [x * Math.cos(turn) - tiltedY * Math.sin(turn),
      x * Math.sin(turn) + tiltedY * Math.cos(turn), z];
  }

  function orbitGeometry(points, lines) {
    const rings = [[1.59, 0.94, 0.19], [1.38, -1.10, 1.04], [1.64, 0.36, -0.57]];
    const segments = 256;
    rings.forEach(function (ring, ringIndex) {
      for (let index = 0; index < segments; index += 1) {
        const angle = index * TAU / segments;
        const next = (index + 1) * TAU / segments;
        const phase = angle + ringIndex * 2.2;
        vertex(lines, orbitPosition(angle, ...ring), 1, 0.38, phase, 1);
        vertex(lines, orbitPosition(next, ...ring), 1, 0.38,
          next + ringIndex * 2.2, 1);
        if (index % 4 === 0) {
          vertex(points, orbitPosition(angle, ...ring), 3.2, 0.72, phase, 1);
        }
      }
    });
  }

  function triangleNormal(a, b, c) {
    const u = b.map((value, index) => value - a[index]);
    const v = c.map((value, index) => value - a[index]);
    const normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
      u[0] * v[1] - u[1] * v[0]];
    const length = Math.hypot(...normal);
    return normal.map((value) => value / length);
  }

  function coreGeometry(points, lines, facets) {
    const phi = (1 + Math.sqrt(5)) / 2;
    const scale = 0.48 / Math.hypot(1, phi);
    const corners = [[-1, phi, 0], [1, phi, 0], [-1, -phi, 0], [1, -phi, 0],
      [0, -1, phi], [0, 1, phi], [0, -1, -phi], [0, 1, -phi],
      [phi, 0, -1], [phi, 0, 1], [-phi, 0, -1], [-phi, 0, 1]]
      .map((position) => position.map((value) => value * scale));
    const faces = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11],
      [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6], [7, 1, 8],
      [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9],
      [4, 9, 5], [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1]];
    const edges = new Set();
    faces.forEach(function (face) {
      const positions = face.map((index) => corners[index]);
      const normal = triangleNormal(...positions);
      positions.forEach((position) => facets.push(...position, ...normal));
      for (let index = 0; index < 3; index += 1) {
        const a = face[index];
        const b = face[(index + 1) % 3];
        const key = a < b ? `${a}:${b}` : `${b}:${a}`;
        if (edges.has(key)) continue;
        edges.add(key);
        vertex(lines, corners[a], 1, 0.50, a * 0.4, 2);
        vertex(lines, corners[b], 1, 0.50, b * 0.4, 2);
      }
    });
    corners.forEach((position, index) => vertex(points, position, 5, 0.82, index, 2));
  }

  // 列主序矩阵；先绕世界 Y 轴 yaw，再绕 X 轴 pitch，与 HTML 节点投影一致。
  function rotationMatrix(yaw, pitch) {
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cx = Math.cos(pitch), sx = Math.sin(pitch);
    return new Float32Array([cy, sx * sy, -cx * sy, 0, 0, cx, sx, 0,
      sy, -sx * cy, cx * cy, 0, 0, 0, 0, 1]);
  }

  function projectionMatrix(aspect) {
    const focal = 1 / Math.tan(Math.PI / 8);
    const near = 0.1, far = 30;
    return new Float32Array([focal / aspect, 0, 0, 0, 0, focal, 0, 0,
      0, 0, (far + near) / (near - far), -1,
      0, 0, 2 * far * near / (near - far), 0]);
  }

  function resourcePool(gl) {
    const entries = [];
    return {
      add(kind, resource) {
        if (!resource) throw new Error("WebGL 无法分配 " + kind + " 资源");
        entries.push({ kind, resource });
        return resource;
      },
      release(kind, resource) {
        const index = entries.findIndex((entry) => entry.resource === resource);
        if (index < 0) return;
        entries.splice(index, 1);
        gl["delete" + kind](resource);
      },
      dispose() {
        while (entries.length) {
          const entry = entries.pop();
          gl["delete" + entry.kind](entry.resource);
        }
      }
    };
  }

  function compileShader(gl, resources, type, source) {
    const shader = resources.add("Shader", gl.createShader(type));
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error("知识球着色器编译失败：" + (gl.getShaderInfoLog(shader) || "未知 GPU 错误"));
    }
    return shader;
  }

  function program(gl, resources, vertexSource, fragmentSource, attributes) {
    const linked = resources.add("Program", gl.createProgram());
    const vertexShader = compileShader(gl, resources, gl.VERTEX_SHADER, vertexSource);
    const fragmentShader = compileShader(gl, resources, gl.FRAGMENT_SHADER, fragmentSource);
    gl.attachShader(linked, vertexShader);
    gl.attachShader(linked, fragmentShader);
    gl.linkProgram(linked);
    if (!gl.getProgramParameter(linked, gl.LINK_STATUS)) {
      throw new Error("知识球着色器链接失败：" + (gl.getProgramInfoLog(linked) || "未知 GPU 错误"));
    }
    [vertexShader, fragmentShader].forEach(function (shader) {
      gl.detachShader(linked, shader);
      resources.release("Shader", shader);
    });
    const uniforms = {};
    ["u_projection", "u_rotation", "u_time", "u_pixelRatio"].forEach(function (name) {
      uniforms[name] = gl.getUniformLocation(linked, name);
    });
    const locations = {};
    attributes.forEach((name) => { locations[name] = gl.getAttribLocation(linked, name); });
    return { linked, uniforms, attributes: locations };
  }

  function buffer(gl, resources, data, dimensions) {
    const resource = resources.add("Buffer", gl.createBuffer());
    gl.bindBuffer(gl.ARRAY_BUFFER, resource);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.STATIC_DRAW);
    return { resource, count: data.length / dimensions };
  }

  function bindProgram(gl, active, projection, rotation, time, ratio) {
    gl.useProgram(active.linked);
    gl.uniformMatrix4fv(active.uniforms.u_projection, false, projection);
    gl.uniformMatrix4fv(active.uniforms.u_rotation, false, rotation);
    if (active.uniforms.u_time !== null) gl.uniform1f(active.uniforms.u_time, time);
    if (active.uniforms.u_pixelRatio !== null) gl.uniform1f(active.uniforms.u_pixelRatio, ratio);
  }

  function bindAttribute(gl, location, size, stride, offset) {
    if (location < 0) return;
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, stride, offset);
  }

  function drawGeometry(gl, active, geometry, type, isFacet) {
    gl.bindBuffer(gl.ARRAY_BUFFER, geometry.resource);
    const stride = isFacet ? 6 * Float32Array.BYTES_PER_ELEMENT : STRIDE;
    bindAttribute(gl, active.attributes.a_position, 3, stride, 0);
    bindAttribute(gl, active.attributes[isFacet ? "a_normal" : "a_meta"],
      isFacet ? 3 : 4, stride, 3 * Float32Array.BYTES_PER_ELEMENT);
    gl.drawArrays(type, 0, geometry.count);
    Object.values(active.attributes).forEach(function (location) {
      if (location >= 0) gl.disableVertexAttribArray(location);
    });
  }

  function create(canvas) {
    if (!canvas || typeof canvas.getContext !== "function") {
      throw new Error("知识球需要有效的 canvas 元素");
    }
    const gl = canvas.getContext("webgl", { alpha: true, antialias: true,
      depth: false, stencil: false, premultipliedAlpha: true,
      preserveDrawingBuffer: false, powerPreference: "low-power" });
    if (!gl) throw new Error("当前设备无法创建 WebGL1 上下文");
    const resources = resourcePool(gl);
    try {
      return initialize(gl, canvas, resources);
    } catch (error) {
      resources.dispose();
      throw error;
    }
  }

  function initialize(gl, canvas, resources) {
    const pointProgram = program(gl, resources, VERTEX, POINT_FRAGMENT, ["a_position", "a_meta"]);
    const lineProgram = program(gl, resources, VERTEX, LINE_FRAGMENT, ["a_position", "a_meta"]);
    const facetProgram = program(gl, resources, FACET_VERTEX, LINE_FRAGMENT, ["a_position", "a_normal"]);
    const points = [], lines = [], facets = [];
    spherePoints(points);
    orbitGeometry(points, lines);
    coreGeometry(points, lines, facets);
    const pointBuffer = buffer(gl, resources, points, 7);
    const lineBuffer = buffer(gl, resources, lines, 7);
    const facetBuffer = buffer(gl, resources, facets, 6);
    let disposed = false;

    function draw(frame) {
      if (disposed) return;
      if (gl.isContextLost()) throw new Error("WebGL 上下文已丢失，知识球需要重建");
      const { width, height } = frame;
      if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;
      const ratio = Math.min(1.5, Math.max(0.1, global.devicePixelRatio || 1),
        Math.sqrt(MAX_PIXELS / (width * height)));
      const pixelWidth = Math.min(MAX_PIXELS, Math.max(1, Math.floor(width * ratio)));
      const pixelHeight = Math.min(Math.floor(MAX_PIXELS / pixelWidth),
        Math.max(1, Math.floor(height * ratio)));
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      const time = Number.isFinite(frame.time) ? frame.time % (TAU * 10000) : 0;
      const rotation = rotationMatrix(Number.isFinite(frame.yaw) ? frame.yaw : 0,
        Number.isFinite(frame.pitch) ? frame.pitch : 0);
      const projection = projectionMatrix(width / height);
      gl.viewport(0, 0, pixelWidth, pixelHeight);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      bindProgram(gl, facetProgram, projection, rotation, time, ratio);
      drawGeometry(gl, facetProgram, facetBuffer, gl.TRIANGLES, true);
      bindProgram(gl, lineProgram, projection, rotation, time, ratio);
      drawGeometry(gl, lineProgram, lineBuffer, gl.LINES, false);
      bindProgram(gl, pointProgram, projection, rotation, time, ratio);
      drawGeometry(gl, pointProgram, pointBuffer, gl.POINTS, false);
    }

    return {
      draw,
      dispose() {
        if (disposed) return;
        disposed = true;
        resources.dispose();
      }
    };
  }

  global.ZhiOrbRenderer = Object.freeze({ create });
})(window);
