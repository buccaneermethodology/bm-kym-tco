# External business workspace handoff


This Skill distribution is versioned independently from the installed CLI. In an external business repository, use the installed `tco` CLI; the project does not need `src/kym_tco`, `governance/supported_host_registry_v1.json`, or the tool's source repository. The host does not automatically attach a pipeline to every Skill invocation: an operator must run the documented commands after both candidate packages exist.

1. Record the raw source inputs in a workspace-relative `source_input_manifest_v1` file and confirm its source refs/digests. Ask the installed CLI to normalize and freeze the exact manifest bytes and current source bytes:

   ```bash
   tco --workspace "$PWD" source-ledger build \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --output .kym-tco/runtime/source-ledgers/<revision>.json
   tco --workspace "$PWD" source-ledger verify \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --ledger .kym-tco/runtime/source-ledgers/<revision>.json
   ```

2. Inspect the host's actual project/global Skill selection. A project copy takes precedence when present; if that selected copy is broken or incompatible, stop and repair it rather than silently choosing global. If the host cannot provide a verifiable resolver attestation, write a short operator evidence note in the workspace and create a `selection-record` with both actual Skill directories, source labels, reasons, operator ID, and evidence ref. `operator_confirmed` is the operator's assertion, not a CLI claim that it observed the host UI. `caller_declared` inputs alone cannot authorize `external-run`.

   ```bash
   tco --workspace "$PWD" selection-record create \
     --build-kym-skill-dir <selected-build-kym-directory> --build-kym-skill-source <project|global> --build-kym-reason '<why this copy was selected>' \
     --build-tco-coverage-skill-dir <selected-build-tco-coverage-directory> --build-tco-coverage-skill-source <project|global> --build-tco-coverage-reason '<why this copy was selected>' \
     --operator-id <operator-id> --evidence-ref .kym-tco/runtime/skill-selection/selection-review.md \
     --output .kym-tco/runtime/skill-selection/current.json
   ```

   Add `--project-copy-exists build-kym` and/or `--project-copy-exists build-tco-coverage` when that project copy exists. The evidence note is written and reviewed by the operator before running the command.
3. Build and package the KYM candidate by following this Skill. Hand off the candidate file refs, source manifest refs, and source boundary to `build-tco-coverage`; do not call the CLI evidence bundle a candidate package.
4. After both candidate packages exist, run `tco host-preflight` for each selected Skill, then the supported deterministic pipeline:

   ```bash
   tco --workspace "$PWD" external-run \
     --host-profile installed_cli_external_workspace_v1 \
     --build-kym-skill-dir <selected-build-kym-directory> --build-kym-skill-source <project|global> \
     --build-tco-coverage-skill-dir <selected-build-tco-coverage-directory> --build-tco-coverage-skill-source <project|global> \
     --source-manifest .kym-tco/<product-id>/source-inputs.json \
     --source-ledger .kym-tco/runtime/source-ledgers/<revision>.json \
     --skill-selection-record .kym-tco/runtime/skill-selection/<selection-id>.json \
     --output <kym-candidate-file> --output <tco-candidate-file> \
     --attempt-id <unique-attempt-id>
   ```

5. The command returns a `bundle_ref` for the CLI **validation evidence bundle**. The Skill-generated **candidate packages** remain separate and unchanged. Supported consumers check the candidate ref and that bundle together:

   ```bash
   tco --workspace "$PWD" external-consume-check \
     --candidate <candidate-file> --evidence <bundle-ref>
   ```

   Only `consumable=true` means that candidate's bytes match a passing deterministic bundle; it remains `candidate_not_approved`. A failed, blocked, unknown, or retained-partial result is not consumable. Read `.kym-tco/runtime/host-failure-records/` and the referenced bundle/disposition; after changing inputs, Skill selection, or candidate bytes, retry with a new attempt ID. A successful gate never means semantic correctness, Coverage Met, approval, or release.


## 源payload与Python运行缓存

Skill身份校验全部发行源文件、manifest与注册期望。标准__pycache__/*.pyc只有能由stdlib source_from_cache对应到同Skill树内实际受验.py源时，才作为衍生运行缓存不计源tree摘要；缓存不是发布源码或已验证代码，官方伴随脚本继续从源执行。所有symlink先拒，源修改、额外源码/非缓存文件、孤儿/旁置缓存和缓存目录中的其他文件继续拒。不得通过删目录、改manifest或忽略任意.pyc凑通过；常规pip安装及伴随调用前后均核验源payload身份。

## 已有KYM revision时的管线基线

external-run会执行G-KYM-PACKAGE，不是只读复用已有初始revision。若已按普通Skill流程生成过初始包，不能把冻结的canonical revision当新初稿再打包，也不能删revision来隐藏状态。现有协议允许在新的候选draft中显式声明base_revision：值为真实现存baseline的裸revision标识，而不是路径；管线据它找到`.kym-tco/<product-id>/kym/revisions/kym-<base_revision>.json`并调用已有--base。

先确认本次确实是基于该baseline的候选/审阅重试，在新draft保留源支持内容、真实lineage和原始义务；冻结revision与旧包不改。source manifest的kym输入引用该新draft，随后重新build/verify source-ledger及本次输入身份。候选revision与TCO来源关系仍须独立核对，技术重试不算新业务实现或Session努力。不得捏造base、静默改旧source-ledger或绕过管线门禁。若没有合法基线/新候选条件，保留该外部链路未通过与已完成的只读成果，不能宣称consumable。
