# 知识档案馆前端

2026-09-11 第五轮。用户要求参考 Kage，并授权生成相关图片。本轮使用完整背景、鼠标视差、滚动推进和直接操作入口；保留原生 HTML/CSS/JavaScript，不引入 Three.js、React 或构建工具。

## 参考与实现

实际查看 [ThreeUI Kage](https://threeui.com/landing-pages/kage.html)，临时下载 HTML 并核对 SHA-256：`c8e06b90397ac246baf0ab6f32f5f6b570acc6fe03c7009f711b579fb72d9f49`。参考其全屏构图、前后景分层、章节滚动与直接导航；项目代码和图片独立实现。临时参考完成后清理，不作为运行资产。

原版采用 Three.js；本版是 **原创位图背景 + CSS 3D 透视薄片 + 分层视差 + Canvas 光尘**。没有将位图称为可自由漫游的三维模型。薄片只作装饰，主要操作不依赖移动目标。

## 用户路径

1. 首屏提交进入原调查表单与 SSE；已有调查进行中时保留原问题，提示等待或停止。
2. 示例问句只填入输入框，用户提交后执行。离线模式明确标记。
3. 向下查看有效已发布 Wiki 的一条结论，打开支撑原文或完整 Wiki。
4. 从导航进入资料、Wiki、切分实验；桌面用侧栏，手机用底部导航。
5. 离线输出按来源展开，另有完整原始输出；原文未被改写，没有增加答案生成逻辑。

路由只接受五个已知视图。通过 pushState/popstate 支持前进后退，保留输入与结果，刷新深链接可进入对应视图，参考 [MDN History API](https://developer.mozilla.org/en-US/docs/Web/API/History/pushState)。快捷键为 Ctrl/⌘ K 提问、Ctrl/⌘ Enter 提交、Escape 收起菜单；详情模态框打开时不抢占提问快捷键。

## 引用真实性

archive-model.js 只选择有效已发布、非 stale 页面的结论。每个 evidence_id 必须由当前原文唯一拥有；缺失、已失效、归属冲突均跳过。同一原文的多个片段合并为一个按钮。标题、结论、版本均来自 API，以 textContent 写入。示例中的虚构标记保留，统计沿用真实 API，装饰建筑数量与资料数无关。

## 动效与成本

透视薄片使用 CSS perspective/translateZ/rotateY，参考 [MDN perspective](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/perspective)。原生滚动决定有限幅度的场景推进，不拦截滚轮、不锁定全屏。

统一持有一个 requestAnimationFrame，按时间差推进，帧间隔最大取 0.05 秒；切出首页、场景离屏、隐藏页面或关闭动效时取消。时间戳用法参考 [MDN requestAnimationFrame](https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame)。Canvas DPR 上限 1.25，缓冲最多约 90 万像素，桌面 36 点、粗指针设备 18 点。Canvas 不可用时，图片和业务入口保持可用。

初始尊重减少动效偏好，显式开关可覆盖初始选择。图片加载不阻断输入；没有开场倒计时。上述是实现约束，未测量多设备 FPS、功耗或长期内存。

## 原创图片与来源

- 内置 Imagegen 生成，PNG，1672×941，1,738,831 字节。
- 项目路径：zhiwiki/web/media/knowledge-archive.png；浏览器本地路径 /assets/media/knowledge-archive.png。
- 画面是装饰性档案馆，屏幕阅读器忽略，不承载事实或按钮。
- 未采用 Kage 的寺庙、植被、字体或其他图片。完整生成提示词见 [ASSET_PROMPT](ASSET_PROMPT.md)。

## 模块与验证

新模块为 archive-model.js、archive.js、archive.css。app.js 添加体验钩子并收紧导航选择器；workspace.js 在数据刷新后更新真实结论。旧球体文件与测试保留为第四轮历史，当前 HTML 不加载它们。

第五轮通过 68 项 Node 测试：本轮新增 12 项、已有业务前端 24 项、旧空间实现回归 32 项；另通过 19 项 HTTP 相关测试。实际桌面和 390×844 手机检查覆盖首屏提问、原文打开、手机菜单与返回、资料筛选及横向溢出。详细结果见 [ACCEPTANCE](ACCEPTANCE.md)。
