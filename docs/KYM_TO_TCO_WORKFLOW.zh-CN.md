# 从 KYM 到 TCO：工作流边界

```text
明确来源
  -> build-kym 起草候选 KYM
  -> kym validate / lint / render
  -> 人类审查（可选批准 baseline/Target）
  -> build-tco-coverage 起草候选 TCO/Coverage
  -> tco validate / verify / render-preview
  -> 人类审查与后续决定
```

Skill 负责语义起草、归类、分解和映射；CLI 负责来源、schema、图结构、证据引用、校验、打包和渲染。通过 CLI 检查只证明对应的确定性检查，不证明语义正确或授权状态。

输入可以是对话、文档、源码观察或已有候选文件。每个输入都应有明确边界和可回读的来源身份；不确定内容应保留为 unresolved/disputed，而不是补写共识。

KYM 是 TCO 起草的输入，不应把 KYM 的 Business Rules 直接当作 TCO capability tree。TCO/Coverage 始终保持 candidate-only，直到用户明确进行独立审查和授权。
