# Contributing to bm-kym-tco

请先说明变更影响的是 CLI、两个公开 Skill、文档还是验证配置，并保持变更可复现。

## 基本要求

- 保持 Python 3.9+ 与 `kym`/`tco` entrypoints 兼容。
- 不把确定性检查、预览或候选状态写成语义批准。
- 新增来源或依赖时保留许可证与 attribution。
- 不提交凭据、私有路径、用户数据、缓存或未公开项目历史。
- Skill 只能覆盖 `build-kym` 或 `build-tco-coverage`；其他 Skill 不属于本公开仓。

## 检查

```bash
python -m pip wheel --no-deps .
python -m kym_tco.kym_cli --help
python -m kym_tco.tco_cli --help
```

首版公开树不携带内部测试 fixture；提交时请说明实际验证范围、已知边界和用户可见变化。
