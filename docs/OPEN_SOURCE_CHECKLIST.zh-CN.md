# v0.4.1 公开版本检查清单

本清单说明公开范围，不等于 PyPI 发布或业务语义批准。

- 仓库：`buccaneermethodology/bm-kym-tco`。
- 身份：project `bm-kym-tco`，import `kym_tco`，命令 `kym|tco`，tag `v0.4.1`。
- 公开 Skill：且仅有 `build-kym`、`build-tco-coverage`。
- 许可证：Apache-2.0；保留 `LICENSE` 与 `NOTICE`。
- 排除：其他 Skills、Dashboard、KB、内部治理记录、测试 fixture、凭据和私有历史。
- 验证：allowlist、CLI help、wheel/sdist、安装、泄漏扫描与 release asset digest。

后续版本应从内部 `kym-tco` 真源按同一 allowlist 生成，并将适配、digest、许可证 disposition 和远端 read-back 作为 release evidence。公开仓不自动反向合并内部治理或历史。
