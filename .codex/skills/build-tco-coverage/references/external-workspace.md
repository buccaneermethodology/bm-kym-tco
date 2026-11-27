# External business workspace handoff

This Skill distribution is independently versioned from the CLI. For an external business repository, use the installed `tco` CLI. The business repo need not contain `src/kym_tco`, the internal host registry, or tool source. Skill invocation does not automatically trigger the pipeline on arbitrary platforms; run the following host calls explicitly after the semantic candidate work is complete.

1. Build the Coverage proposal, validate it, and package it as a Skill-generated candidate package. Preserve source/Target boundaries and do not treat CLI validation as semantic synthesis.
2. Build and verify the source ledger against one or more raw manifests and current workspace bytes:

   ```bash
   tco --workspace "$PWD" source-ledger build \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --output .kym-tco/runtime/source-ledgers/<revision>.json
   tco --workspace "$PWD" source-ledger verify \
     --input-manifest .kym-tco/<product-id>/source-inputs.json \
     --ledger .kym-tco/runtime/source-ledgers/<revision>.json
   ```

   Repeat `--input-manifest` for additional manifests. The ledger retains each raw manifest byte digest, declared denominator, normalization rule version, and actual source bytes. `external-run` repeats this check; changing a manifest or source invalidates the ledger.
3. Inspect the host's actual project/global Skill selection. If a project Skill exists, it takes precedence; a broken or incompatible project copy blocks the run and must not fall back to global. When no verifiable host resolver attestation is available, save a short operator evidence note in the workspace, then create a durable `selection-record` with both selected directories, source labels, reasons, operator ID, and evidence ref. `operator_confirmed` records the operator's assertion and does not claim the CLI observed the UI. Caller-declared paths alone are diagnostic and cannot authorize the run.

   ```bash
   tco --workspace "$PWD" selection-record create \
     --build-kym-skill-dir <selected-build-kym-directory> --build-kym-skill-source <project|global> --build-kym-reason '<why this copy was selected>' \
     --build-tco-coverage-skill-dir <selected-build-tco-coverage-directory> --build-tco-coverage-skill-source <project|global> --build-tco-coverage-reason '<why this copy was selected>' \
     --operator-id <operator-id> --evidence-ref .kym-tco/runtime/skill-selection/selection-review.md \
     --output .kym-tco/runtime/skill-selection/current.json
   ```

   Add `--project-copy-exists build-kym` and/or `--project-copy-exists build-tco-coverage` when that project copy exists. The operator writes and reviews the evidence note before recording the assertion.
4. Run `tco host-preflight` for each actual selected Skill directory. It only verifies installed runtime/profile and Skill identity; it does not run candidate gates. Then explicitly invoke the deterministic pipeline:

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

5. The `bundle_ref` returned by the CLI names the **validation evidence bundle**, not another candidate package. A supported consumer verifies each candidate against that bundle:

   ```bash
   tco --workspace "$PWD" external-consume-check \
     --candidate <candidate-file> --evidence <bundle-ref>
   ```

   `consumable=true` means the referenced bytes match a passing deterministic validation bundle and still have lifecycle `candidate_not_approved`. Failure, blocked, unknown evidence, or retained partial output is not consumable. Inspect `.kym-tco/runtime/host-failure-records/` and bundle disposition; change the attempt ID for every retry after changing source, Skills, versions, or candidate bytes. A pass does not mean semantic correctness, Coverage Met, approval, promotion, or release.


## 源payload与Python运行缓存

Skill身份校验全部发行源文件、manifest与注册期望。标准__pycache__/*.pyc只有能由stdlib source_from_cache对应到同Skill树内实际受验.py源时，才作为衍生运行缓存不计源tree摘要；缓存不是发布源码或已验证代码，官方伴随脚本继续从源执行。所有symlink先拒，源修改、额外源码/非缓存文件、孤儿/旁置缓存和缓存目录中的其他文件继续拒。不得通过删目录、改manifest或忽略任意.pyc凑通过；常规pip安装及伴随调用前后均核验源payload身份。

## 已有KYM revision时的管线基线

external-run会执行G-KYM-PACKAGE，不是只读复用已有初始revision。若已按普通Skill流程生成过初始包，不能把冻结的canonical revision当新初稿再打包，也不能删revision来隐藏状态。现有协议允许在新的候选draft中显式声明base_revision：值为真实现存baseline的裸revision标识，而不是路径；管线据它找到`.kym-tco/<product-id>/kym/revisions/kym-<base_revision>.json`并调用已有--base。

先确认本次确实是基于该baseline的候选/审阅重试，在新draft保留源支持内容、真实lineage和原始义务；冻结revision与旧包不改。source manifest的kym输入引用该新draft，随后重新build/verify source-ledger及本次输入身份。候选revision与TCO来源关系仍须独立核对，技术重试不算新业务实现或Session努力。不得捏造base、静默改旧source-ledger或绕过管线门禁。若没有合法基线/新候选条件，保留该外部链路未通过与已完成的只读成果，不能宣称consumable。
