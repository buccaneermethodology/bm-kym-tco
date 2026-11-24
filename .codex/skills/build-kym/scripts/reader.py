#!/usr/bin/env python3
"""只读、离线、可搬移的 KYM/TCO 候选阅读伴随工具。"""
from __future__ import annotations
import argparse
import base64
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import quote, unquote, urlsplit

VERSION = 'reader-v1.0.0'
SKILL = Path(__file__).resolve().parents[1]
GAPS = {'implementation': '实现缺口', 'verification': '验证缺口', 'version_applicability': '版本适用性缺口', 'evidence_binding': '证据绑定缺口', 'business_question': '业务待确认', 'not_evaluated': '未评估', 'none': '未报告主要缺口（不是批准）'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def esc(value):
    return html.escape(str(value), quote=True)


def anchor(value):
    return 'a-' + digest(str(value).encode('utf-8'))[:20]


def read_json(path, payload=None):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON 包含重复字段：' + key)
            result[key] = value
        return result
    return json.loads((payload if payload is not None else path.read_bytes()).decode('utf-8'), object_pairs_hook=pairs)


def schema_errors(value, schema, path='$'):
    """校验冻结 schema 使用的关键字；拒绝静默忽略未来新增的关键字。"""
    supported = {'$schema', 'type', 'additionalProperties', 'properties', 'required', 'items', 'minLength', 'minItems', 'pattern', 'const', 'enum'}
    if set(schema) - supported:
        raise ValueError('reader schema 使用未支持关键字：' + ', '.join(sorted(set(schema) - supported)))
    types = {'object': dict, 'array': list, 'string': str}
    typ = schema.get('type')
    if typ and not isinstance(value, types[typ]):
        return [path + ' 类型应为 ' + typ]
    errors = []
    if 'const' in schema and value != schema['const']:
        errors.append(path + ' 常量不匹配')
    if 'enum' in schema and value not in schema['enum']:
        errors.append(path + ' 枚举不匹配')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or (schema.get('minLength', 0) and not value.strip()):
            errors.append(path + ' 不得为空')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(path + ' 格式不匹配')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            errors.append(path + ' 元素不足')
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema.get('items', {}), path + '/' + str(index)))
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        errors.extend(path + '/' + k + ' 缺失' for k in schema.get('required', []) if k not in value)
        for key, item in value.items():
            if key in properties:
                errors.extend(schema_errors(item, properties[key], path + '/' + key))
            elif schema.get('additionalProperties') is False:
                errors.append(path + '/' + key + ' 未允许的字段')
            elif isinstance(schema.get('additionalProperties'), dict):
                errors.extend(schema_errors(item, schema['additionalProperties'], path + '/' + key))
    return errors


def local_path(workspace, value, must_exist=True):
    if not isinstance(value, str) or not value or value != unquote(value):
        raise ValueError('链接必须是未编码的工作区相对路径')
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or PurePosixPath(value).is_absolute()
            or '\\' in value or any(p in ('..', '.') for p in value.split('/'))
            or any(ord(ch) < 32 for ch in value) or ':' in value):
        raise ValueError('不安全的相对路径：' + value)
    target = (workspace / value).resolve()
    try:
        target.relative_to(workspace)
    except ValueError:
        raise ValueError('路径越出工作区：' + value)
    if must_exist and not target.is_file():
        raise ValueError('来源文件不存在：' + value)
    return target


def tree_nodes(tree):
    result = {}
    def walk(node):
        if not isinstance(node, dict) or not isinstance(node.get('id'), str):
            raise ValueError('KYM 树节点缺少 id')
        if node['id'] in result:
            raise ValueError('KYM 树节点 id 重复：' + node['id'])
        result[node['id']] = node
        for child in node.get('children', []):
            walk(child)
    walk(tree)
    return result


def load_inputs(args):
    workspace = args.workspace.resolve()
    paths = {'detail': local_path(workspace, args.detail)}
    payloads = {'detail': paths['detail'].read_bytes()}
    data = {'detail': read_json(paths['detail'], payloads['detail']), '_input_bytes': payloads}
    detail = data['detail']
    errors = schema_errors(detail, read_json(SKILL / 'references/reader-detail.schema.json'))
    if errors:
        raise ValueError('detail schema 校验失败：' + '; '.join(errors))
    if args.coverage and not args.model:
        raise ValueError('Coverage 必须伴随 --model')
    for key, value in {'kym': args.kym, 'model': args.model, 'coverage': args.coverage}.items():
        binding = detail['bindings'].get(key)
        if not value:
            if binding:
                raise ValueError('存在未提供的输入绑定：' + key)
            continue
        path = local_path(workspace, value)
        if not binding or local_path(workspace, binding['path']) != path:
            raise ValueError('输入绑定路径不匹配：' + key)
        payloads[key] = path.read_bytes()
        if digest(payloads[key]) != binding['sha256']:
            raise ValueError('输入版本摘要漂移：' + key)
        paths[key], data[key] = path, read_json(path, payloads[key])
    if data['kym'].get('schema') != 'kym_tco.kym_model.v1':
        raise ValueError('仅支持 KYM v1 候选输入')
    if 'model' in data:
        model = data['model']
        if model.get('schema') not in {'kym_tco.tco_model.v2', 'kym_tco.tco_model.v3', 'kym_tco.tco_model.v4'}:
            raise ValueError('仅支持 TCO v2/v3/v4 候选输入')
        if model.get('product_id') != data['kym'].get('product_id') or model.get('source_kym_revision') != data['kym'].get('revision'):
            raise ValueError('TCO 与 KYM 的产品或版本不一致')
    if 'coverage' in data:
        coverage = data['coverage']
        if coverage.get('schema') not in {'kym_tco.tco_coverage_model.v2', 'kym_tco.tco_coverage_model.v3'}:
            raise ValueError('仅支持 Coverage v2/v3 候选输入')
        if coverage.get('product_id') != data['model'].get('product_id'):
            raise ValueError('Coverage 产品不一致')
        if coverage.get('source_tco_model_digest') != 'sha256:' + digest(json.dumps(data['model'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')):
            raise ValueError('Coverage 的模型摘要漂移')
    return workspace, paths, data


def has_rule_statements(node):
    return bool(node.get('statement_refs')) or any(has_rule_statements(child) for child in node.get('children', []))


def empty_rule_branches(kym):
    empty = []
    def collect(node):
        if not has_rule_statements(node):
            empty.append(node)
            return
        for child in node.get('children', []):
            collect(child)
    for node in tree_nodes(kym['tree']).values():
        if node['id'] == 'business_rules' or node.get('title', '').lower() == 'business rules':
            collect(node)
    return empty


def audit(workspace, paths, data):
    detail, kym = data['detail'], data['kym']
    tree = tree_nodes(kym['tree'])
    statements = {s['id']: s for s in kym.get('statements', [])}
    findings = []
    def finding(code, message, subject='', severity='error'):
        findings.append({'code': code, 'severity': severity, 'subject_id': subject, 'message': message})
    model = data.get('model')
    if model:
        all_nodes = model.get('nodes', [])
        nodes = {n['id']: n for n in all_nodes}
        if len(nodes) != len(all_nodes):
            raise ValueError('TCO 节点 id 重复')
        capabilities = {k: n for k, n in nodes.items() if n.get('node_kind') == 'm1'}
        def visit(key, stack):
            if key in stack:
                raise ValueError('TCO 分组图存在循环：' + key)
            for child in nodes[key].get('children', []):
                if child not in nodes:
                    finding('UNKNOWN_CHILD', '存在未知子节点：' + child, key)
                else:
                    visit(child, stack | {key})
        for key in nodes:
            visit(key, set())
    else:
        nodes, rule_ids = tree, set()
        def collect(node):
            if not has_rule_statements(node):
                return
            if node['id'] in detail['capabilities']:
                rule_ids.add(node['id'])
                return
            if not node.get('children'):
                rule_ids.add(node['id'])
            for child in node.get('children', []):
                collect(child)
        for node in tree.values():
            if node['id'] == 'business_rules' or node.get('title', '').lower() == 'business rules':
                collect(node)
        capabilities = {k: tree[k] for k in rule_ids}
    for key in detail['capabilities']:
        if key not in capabilities:
            finding('UNKNOWN_CAPABILITY', '详细能力必须是实际 M/Business Rules 节点；Mc 只分组', key)
    for key in capabilities:
        if key not in detail['capabilities']:
            finding('MISSING_DETAIL', '缺少详细阅读内容，保持未评估', key)
    rule_sources = {}
    def rule_support(node, fixed_root=False):
        refs = {ref for ref in node.get('statement_refs', []) if ref in statements}
        own = bool(refs)
        for child in node.get('children', []):
            refs.update(rule_support(child))
        if refs and (not fixed_root or own):
            rule_sources[node['id']] = refs
        return refs
    for branch in tree.values():
        if branch['id'] == 'business_rules' or branch.get('title', '').lower() == 'business rules':
            rule_support(branch, fixed_root=True)
    for projection in detail['projections']:
        source = projection['kym_node_id']
        if source not in tree:
            finding('UNKNOWN_KYM_MAPPING', '映射指向不存在的 KYM 节点', source)
        elif source not in rule_sources:
            finding('INVALID_KYM_RULE_SOURCE', '映射必须指向有来源陈述支撑的 Business Rules 语义分支，不能使用根节点/角色/空分支', source)
        for key in projection['tco_node_ids']:
            if not model or key not in capabilities:
                finding('UNKNOWN_M_MAPPING', '映射必须指向实际 M 而非 Mc', key)
    if model:
        mapped = {k for p in detail['projections'] for k in p['tco_node_ids']}
        for key in capabilities.keys() - mapped:
            finding('MISSING_PROJECTION', 'M 缺少 KYM 规则追溯映射', key)
    if model:
        profile_ids = set()
        target_ids = {p['id'] for p in kym.get('target_profiles', [])}
        for profile in model.get('target_profiles', []):
            if profile['id'] in profile_ids:
                finding('DUPLICATE_PROFILE', '目标 id 重复', profile['id'])
            profile_ids.add(profile['id'])
            if profile.get('source_target_id') not in target_ids:
                finding('UNKNOWN_TARGET_SOURCE', 'TP 未追溯至实际 KYM Profile', profile['id'])
            for requirement in profile.get('requirements', []):
                for key in requirement.get('node_ids', []):
                    if key not in nodes:
                        finding('UNKNOWN_PROFILE_NODE', 'TP 指向不存在的节点：' + key, profile['id'])
    evidence_paths, records = {}, {}
    for record in data.get('coverage', {}).get('records', []):
        key = record.get('tco_node_id')
        if key not in capabilities or key in records:
            finding('INVALID_COVERAGE_RECORD', 'Coverage 节点必须是唯一实际 M', str(key))
        records[key] = record
    for key, cap in detail['capabilities'].items():
        if key not in capabilities:
            continue
        steps = [s['id'] for s in cap['process']]
        if len(set(steps)) != len(steps):
            finding('DUPLICATE_STEP', '流程步骤 id 重复', key)
        for step in cap['process']:
            for edge in step['transitions']:
                if edge['to'] not in steps:
                    finding('UNKNOWN_TRANSITION', '条件转移目标不存在：' + edge['to'], key)
        node = capabilities[key]
        known = {r['id'] for r in node.get('coverage_requirements', [])}
        mandatory = set(node.get('mandatory_coverage_requirement_ids', [])) | {r['id'] for r in node.get('coverage_requirements', []) if r.get('mandatory')}
        mapped, row_ids = set(), set()
        for row in cap['assessment']:
            if row['id'] in row_ids:
                finding('DUPLICATE_BEHAVIOR', '行为 id 重复', key)
            row_ids.add(row['id'])
            mapped.update(row['requirement_ids'])
            if not row['requirement_ids'] and row['gap_kind'] not in {'not_evaluated', 'business_question'}:
                finding('UNMAPPED_BEHAVIOR', '无 requirement 映射只能保持未评估或业务待确认', key)
            for req in row['requirement_ids']:
                if not model or req not in known:
                    finding('UNKNOWN_REQUIREMENT', '行为映射不是本 M 的真实 coverage requirement：' + req, key)
            if row['gap_kind'] == 'none':
                record = records.get(key, {})
                requirement_records = {r.get('requirement_id'): r for r in record.get('requirement_coverage', [])}
                missing = set(record.get('missing_coverage_requirement_ids', []))
                unsupported = (not row['evidence_refs'] or not record or data.get('coverage', {}).get('coverage_mode') == 'not_evaluated'
                               or any(req not in requirement_records or requirement_records[req].get('coverage_state') == 'not_evaluated' or req in missing for req in row['requirement_ids']))
                if unsupported:
                    finding('UNSUPPORTED_NO_GAP', '缺少本行为的 Coverage/证据绑定，不能显示为已证明', key)
            for ref in row['evidence_refs']:
                try:
                    target = local_path(workspace, ref, must_exist=False)
                    if target.is_file():
                        evidence_paths[ref] = target
                    else:
                        finding('UNRESOLVED_EVIDENCE', '证据未解析为本地文件，仅保留文本：' + ref, key, 'warning')
                except ValueError as exc:
                    finding('UNSAFE_LINK', str(exc), key)
        if key in records:
            record = records[key]
            record_req_ids = {r.get('requirement_id') for r in record.get('requirement_coverage', [])}
            for req in record_req_ids - known:
                finding('UNKNOWN_COVERAGE_REQUIREMENT', 'Coverage 含本 M 不存在的 requirement：' + str(req), key)
        if not cap['assessment']:
            finding('EMPTY_ASSESSMENT', '未提供逐行为判断，保持未评估', key)
        if not cap['inputs'] or not cap['outputs']:
            finding('INCOMPLETE_IPO', '输入或输出未明确', key)
        if 'coverage' in data and data['coverage'].get('coverage_mode') != 'not_evaluated' and mandatory - mapped:
            finding('UNMAPPED_MANDATORY', 'mandatory requirement 未映射：' + ', '.join(sorted(mandatory - mapped)), key)
    if detail['intake']['status'] != 'answered':
        finding('INTAKE_PENDING', '目的和范围含假设或待确认，阅读结果不等于人类回答', severity='warning')
    return findings, nodes, capabilities, evidence_paths, records, tree, statements


def items(values):
    return '<ul>' + ''.join('<li>' + esc(v) + '</li>' for v in values) + '</ul>' if values else '<p class="muted">未提供，待确认</p>'


def scope_sections(node):
    """仅投影来源版本的 Scope；旧字符串不是 v4 requirement 分母。"""
    scope = node.get('scope', {})
    if isinstance(scope, list):
        return [('旧版 Scope 原始条目（保留原义，不转换为 coverage requirement）', scope),
                ('旧版 mandatory_scope_items（原始必需范围）', node.get('mandatory_scope_items', [])),
                ('旧版 excluded_outputs（原始排除输出）', node.get('excluded_outputs', []))]
    boundary = scope.get('boundary', {})
    return [('包含', boundary.get('included', [])), ('不包含', boundary.get('excluded', []))]


def diagram_html(diagram):
    out = '<figure class="diagram ' + diagram['kind'] + '"><figcaption>' + esc(diagram['title']) + '</figcaption>'
    for line in diagram['content'].splitlines():
        if line.strip():
            chunks = line.split('->') if diagram['kind'] == 'modeling' else [line]
            out += '<div class="diagram-row">' + '<span class="diagram-arrow" aria-label="连接">→</span>'.join('<div class="diagram-box">' + esc(c.strip()) + '</div>' for c in chunks) + '</div>'
    return out + '</figure>'


def reader_structure(data, nodes, caps):
    kym, model = data['kym'], data.get('model')
    profiles = (model or kym).get('target_profiles', [])
    def members(profile):
        if model:
            seen, pending = set(), [k for r in profile.get('requirements', []) for k in r.get('node_ids', [])]
            while pending:
                key = pending.pop()
                if key not in seen and key in nodes:
                    seen.add(key)
                    pending.extend(nodes[key].get('children', []))
            return sorted(seen & caps.keys())
        refs = set(profile.get('requirement_refs', []))
        def matches(node):
            return bool(refs & set(node.get('statement_refs', []))) or any(matches(c) for c in node.get('children', []))
        return sorted(k for k, n in caps.items() if matches(n))
    membership = {p['id']: members(p) for p in profiles}
    def group_for(key):
        if not model:
            return 'Business Rules（业务规则）'
        groups = [n['title'] + ' (' + n['id'] + ')' for n in nodes.values() if n.get('node_kind') == 'mc' and key in n.get('children', [])]
        return ' / '.join(groups) if groups else '未分组 M'
    return profiles, membership, group_for


def md_text(value):
    text = html.escape(str(value), quote=True)
    text = text.replace('\\', '\\\\')
    for char in '`*_[]()|#':
        text = text.replace(char, '\\' + char)
    return text.replace('\r', '').replace('\n', '<br>')


def mermaid_label(value):
    text = html.escape(str(value), quote=True).replace('[', '&#91;').replace(']', '&#93;')
    return ' '.join(text.split())


def capability_identity(data, nodes, records, key):
    payload = {'node': nodes[key], 'detail': data['detail']['capabilities'].get(key), 'coverage': records.get(key)}
    return digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))


def capability_gaps(detail, findings, key):
    gaps = [f['message'] for f in findings if f['subject_id'] == key and f['severity'] == 'error']
    gaps += [GAPS[r['gap_kind']] for r in detail['capabilities'].get(key, {}).get('assessment', []) if r['gap_kind'] != 'none']
    return '；'.join(gaps) or '未报告主要缺口（仍须核对证据适用性）'


def markdown_page(data, report, audited, links):
    findings, nodes, caps, _, records, _, statements = audited
    detail, kym, model = data['detail'], data['kym'], data.get('model')
    profiles, membership, group_for = reader_structure(data, nodes, caps)
    out = ['# ' + md_text(kym.get('product_id', '候选')) + ' · ' + ('TCO' if model else 'KYM') + ' 详细阅读',
           '', '本文件为候选阅读伴随，不创建业务批准、覆盖接受态或运行保证。', '', '## 阅读目的与范围', '',
           md_text(detail['intake']['purpose']), '', '读者：' + md_text(detail['intake']['audience']) + '；模式：' + md_text(detail['intake']['mode']) + '；回答状态：' + md_text(detail['intake']['status']) + '（answered=已回答；assumed=假设；pending=待确认）。', '', '### 假设', '']
    add = out.append
    def bullets(values, empty='未提供，待确认。'):
        if values:
            out.extend('- ' + md_text(value) for value in values)
        else:
            add(empty)
        add('')
    bullets(detail['intake']['assumptions'], '未记录假设。')
    add('## 阅读质量与证据边界\n')
    add('错误 ' + str(report['error_count']) + '；提醒 ' + str(report['warning_count']) + '。伴随检查不替代候选包 schema/语义门禁。\n')
    bullets([f['code'] + ' · ' + f['subject_id'] + ' · ' + f['message'] for f in findings], '未发现伴随结构错误；业务语义仍须独立审查。')
    add('## 目标 TP → 分组 Mc → 共享能力 M\n')
    add('TP 数 ' + str(len(profiles)) + '；唯一 M/规则数 ' + str(len(caps)) + '。Mc 只分组，不作为覆盖分母；此处不计算覆盖率。\n')
    if not profiles:
        add('来源未提供 Target Profile；只展示能力与待确认项。\n')
    if not model:
        for branch in empty_rule_branches(kym):
            add('### ' + md_text(branch.get('title', branch['id'])) + '（空分支，待确认）\n')
            add('来源未提供业务规则条目；保持空分支，不虚构能力或详细内容。\n')
    def render_capability(key, namespace, level=5):
        node = caps[key]
        view_key = namespace + '/' + key
        add('<a id="' + anchor(view_key) + '"></a>\n')
        add('#' * level + ' ' + md_text(node.get('title', key)) + ' · ' + md_text(key) + '\n')
        add('共享 M/规则：' + md_text(key) + '；定义摘要：' + capability_identity(data, nodes, records, key) + '\n')
        add('本 M：' + md_text(capability_gaps(detail, findings, key)) + '\n')
        add('分组：' + md_text(group_for(key)) + '\n')
        if model:
            add('**Topic（主题）：** ' + md_text(node.get('topic', '未提供')) + '\n')
            add('**Scope（范围）：**\n')
            for label, values in scope_sections(node):
                add(label + '：\n')
                bullets(values)
            add('**Purpose（目的）：** ' + md_text(node.get('purpose', '未提供')) + '\n')
            for projection in detail['projections']:
                if key in projection['tco_node_ids']:
                    add('KYM 追溯：' + md_text(projection['kym_node_id']) + ' → ' + md_text(projection['relation']) + '；理由：' + md_text(projection['rationale']) + '\n')
        else:
            add('#' * (level + 1) + ' 来源业务规则、TSP 子树与支持状态\n')
            def source_lines(branch, depth=0):
                add('  ' * depth + '- ' + md_text(branch.get('title', branch['id'])))
                for ref in branch.get('statement_refs', []):
                    if ref in statements:
                        add('  ' * (depth + 1) + '- ' + md_text(statements[ref]['text'] + ' · ' + statements[ref].get('support_status', '未提供')))
                for child in branch.get('children', []):
                    source_lines(child, depth + 1)
            source_lines(node)
            add('')
        add('#' * (level + 1) + ' 候选 Coverage 原始状态\n')
        record = records.get(key)
        if record:
            add('| 独立状态轴 | 候选原始值 |\n| --- | --- |')
            for field in ['coverage_state', 'evidence_peak_state', 'effective_state', 'claim_state', 'capability_lifecycle_state', 'validation_posture', 'resolution_status', 'claim_ceiling']:
                if field in record:
                    add('| ' + md_text(field) + ' | ' + md_text(record[field]) + ' |')
            add('')
            if 'scope_coverage' in record or 'missing_scope_items' in record:
                add('旧版 Scope Coverage（原始候选记录，不转换为 v4 requirement 或迁移状态）：\n')
                add('| Scope 原文 | 候选覆盖状态 | 原始说明 |\n| --- | --- | --- |')
                for row in record.get('scope_coverage', []):
                    add('| ' + md_text(row['scope']) + ' | ' + md_text(row['coverage_state']) + ' | ' + md_text(row['notes']) + ' |')
                add('\n旧版 missing_scope_items（原始缺失范围）：\n')
                bullets(record.get('missing_scope_items', []), '来源记录未列缺失范围；不能据此批准覆盖。')
        else:
            add('not_evaluated（未评估）：未提供本 M 的 Coverage；KYM 不赋覆盖状态。\n')
        cap = detail['capabilities'].get(key)
        if not cap:
            add('详细内容缺失；未评估，不能推导已覆盖。\n')
            return
        add('#' * (level + 1) + ' 输入 Input\n')
        bullets(cap['inputs'])
        add('#' * (level + 1) + ' 流程图（与 Process 同源）\n')
        add('```mermaid\nflowchart TD')
        for step in cap['process']:
            node_id = 'n' + anchor(view_key + '/' + step['id'])[2:]
            add('  ' + node_id + '["' + mermaid_label(step['label']) + '"]')
            for edge in step['transitions']:
                add('  ' + node_id + ' -->|"' + mermaid_label(edge['condition']) + '"| n' + anchor(view_key + '/' + edge['to'])[2:])
        add('```\n')
        add('#' * (level + 1) + ' 处理 Process\n')
        for index, step in enumerate(cap['process'], 1):
            add(str(index) + '. <a id="' + anchor(view_key + '/' + step['id']) + '"></a> **' + md_text(step['label']) + '**：' + md_text(step['description']))
            if step['transitions']:
                out.extend('   - ' + md_text(edge['condition'] + ' → ' + edge['to']) for edge in step['transitions'])
            else:
                add('   - 终止步骤，无后继转移。')
        add('')
        add('#' * (level + 1) + ' 输出 Output\n')
        bullets(cap['outputs'])
        add('#' * (level + 1) + ' 代表例子\n')
        bullets(cap['examples'])
        for index, diagram in enumerate(cap['diagrams']):
            add('#' * (level + 1) + ' ' + md_text(diagram['title']) + ('（模型图）' if diagram['kind'] == 'modeling' else '（界面区块图）') + '\n')
            add('```mermaid\nflowchart TD')
            serial = 0
            for line in diagram['content'].splitlines():
                if not line.strip():
                    continue
                chunks = line.split('->') if diagram['kind'] == 'modeling' else [line]
                ids = []
                for chunk in chunks:
                    diagram_id = 'd' + anchor(view_key + '/diagram/' + str(index) + '/' + str(serial))[2:]
                    serial += 1
                    add('  ' + diagram_id + '["' + mermaid_label(chunk.strip()) + '"]')
                    ids.append(diagram_id)
                for left, right in zip(ids, ids[1:]):
                    add('  ' + left + ' --> ' + right)
            add('```\n')
        add('#' * (level + 1) + ' 本 M 逐行为判断\n')
        add('主要下一步分类不覆盖全部轴；结合适用边界与证据说明保留其他同时存在的缺口。建议动作不产生调度、测试义务或状态提升。\n')
        add('| 应证明的行为 / requirement | 已有证据与适用边界 | 主要缺口 | 最小建议动作 / 验收判据 |\n| --- | --- | --- | --- |')
        states = {r.get('requirement_id'): r.get('coverage_state', 'not_evaluated') for r in records.get(key, {}).get('requirement_coverage', [])}
        for row in cap['assessment']:
            row_states = '；'.join(req + ': ' + str(states.get(req, 'not_evaluated（未评估）')) for req in row['requirement_ids']) or 'unmapped（未映射）'
            refs = ['[' + md_text(ref) + '](' + links[ref] + ')' if ref in links else md_text(ref) for ref in row['evidence_refs']]
            add('| ' + md_text(row['behavior']) + '<br>' + md_text(', '.join(row['requirement_ids']) or 'unmapped（未映射，不能视为已覆盖）') + ' | ' + md_text(row['evidence_summary']) + '<br>适用边界：' + md_text(row['applicability']) + '<br>候选逐 requirement 状态：' + md_text(row_states) + '<br>' + '<br>'.join(refs) + ' | ' + md_text(GAPS[row['gap_kind']]) + ' | ' + md_text(row['next_action']) + '<br>验收：' + md_text(row['acceptance']) + ' |')
        add('')
        add('#' * (level + 1) + ' 本 M 业务问题\n')
        bullets(cap['questions'], '本 M 未记录业务问题。')
    for profile in profiles:
        add('### ' + md_text(profile['label']) + '\n')
        add(md_text(profile.get('objective', profile.get('description', ''))) + '\n')
        add('声明上限：' + md_text(profile.get('claim_ceiling', '未提供')) + '\n')
        groups = {}
        for key in membership[profile['id']]:
            groups.setdefault(group_for(key), []).append(key)
        for label, keys in groups.items():
            add('#### ' + md_text(label) + '\n')
            for key in keys:
                render_capability(key, 'tp/' + profile['id'] + '/mc/' + label)
            add('')
        add('同目标其他 M 的缺口见各自条目；全局问题见独立区域。\n')
    unassigned = sorted(caps.keys() - {key for keys in membership.values() for key in keys})
    if unassigned:
        add('## 尚未归入目标的能力\n')
        for key in unassigned:
            render_capability(key, 'unassigned', 3)
    add('## 全局问题（不归入任一 TP/M）\n')
    bullets(detail['global_questions'] + [q['summary'] for q in kym.get('unresolved_questions', []) if q.get('status') == 'open'], '未记录全局问题。')
    add('## 建议目标切片（候选，未写入 canonical TP）\n')
    for profile in detail['intake']['proposed_profiles']:
        add('### ' + md_text(profile['label']) + '\n')
        add(md_text(profile['objective']) + '\n')
        add('推荐理由：' + md_text(profile['rationale']) + '\n')
    add('## 来源与模板摘要\n')
    add(md_text(VERSION) + '；模板摘要：' + report['template_sha256'] + '\n')
    for key, binding in detail['bindings'].items():
        add('- [' + md_text(key + ': ' + binding['path']) + '](' + links[binding['path']] + ') · sha256:' + binding['sha256'])
    add('\n详细伴随：[reader-detail.json](sources/reader-detail.json)；检查：[reader-report.json](reader-report.json)。\n')
    return '\n'.join(out)


def render_page(data, report, audited, links):
    findings, nodes, caps, _, records, _, statements = audited
    detail, kym, model = data['detail'], data['kym'], data.get('model')
    profiles, membership, group_for = reader_structure(data, nodes, caps)
    css = (SKILL / 'assets/reader.css').read_text(encoding='utf-8')
    js = (SKILL / 'assets/reader.js').read_text(encoding='utf-8')
    js_hash = base64.b64encode(hashlib.sha256(js.encode('utf-8')).digest()).decode('ascii')
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'sha256-' + js_hash + '\'; img-src \'none\'; base-uri \'none\'; form-action \'none\'">',
           '<title>' + esc(kym.get('product_id', '候选')) + ' · ' + ('TCO' if model else 'KYM') + ' 阅读</title><style>' + css + '</style></head><body>',
           '<header><p class="eyebrow">KYM / TCO · 候选阅读伴随</p><h1>' + esc(kym.get('product_id', '候选')) + ' · ' + ('TCO' if model else 'KYM') + '</h1><p>结构校验与阅读展示不创建业务批准、覆盖接受态或运行保证。</p></header><main>']
    add = out.append
    intake = detail['intake']
    add('<section><h2>阅读目的与范围</h2><p>' + esc(intake['purpose']) + '</p><p>读者：' + esc(intake['audience']) + '；模式：' + esc(intake['mode']) + '；回答状态：' + esc(intake['status']) + '（answered=已回答；assumed=假设；pending=待确认）。</p><h3>假设</h3>' + items(intake['assumptions']) + '</section>')
    add('<section><h2>阅读质量与证据边界</h2><p>错误 ' + str(report['error_count']) + '；提醒 ' + str(report['warning_count']) + '。伴随检查不替代候选包 schema/语义门禁。</p>' + (items([f['code'] + ' · ' + f['subject_id'] + ' · ' + f['message'] for f in findings]) if findings else '<p>未发现伴随结构错误；业务语义仍须独立审查。</p>') + '</section>')
    add('<section><h2>查找与筛选</h2><label>全文搜索 <input id="search" type="search" placeholder="能力、行为、证据"></label><label>目标 TP <select id="tp-filter"><option value="">全部目标</option>' + ''.join('<option value="' + esc(p['id']) + '">' + esc(p['label']) + '</option>' for p in profiles) + '</select></label><label>能力 M <select id="m-filter"><option value="">全部能力</option>' + ''.join('<option value="' + esc(k) + '">' + esc(n.get('title', k)) + '</option>' for k, n in sorted(caps.items())) + '</select></label><button id="expand" type="button">展开当前能力</button><p id="filter-count" aria-live="polite"></p></section>')
    add('<section><h2>目标 TP → 分组 Mc → 共享能力 M</h2><p>TP 数 ' + str(len(profiles)) + '；唯一 M/规则数 ' + str(len(caps)) + '。Mc 只分组，不作为覆盖分母；此处不计算覆盖率。</p>')
    if not profiles:
        add('<p class="notice">来源未提供 Target Profile；以下只展示能力与待确认项。</p>')
    if not model:
        for branch in empty_rule_branches(kym):
            add('<details class="empty-rule" open><summary>' + esc(branch.get('title', branch['id'])) + '（空分支，待确认）</summary><p>来源未提供业务规则条目；保持空分支，不虚构能力或详细内容。</p></details>')
    def render_capability(key, namespace, tps):
        node = caps[key]
        view_key = namespace + '/' + key
        cap = detail['capabilities'].get(key)
        add('<details class="capability" id="' + anchor(view_key) + '" data-m="' + esc(key) + '" data-tps="' + esc(json.dumps(tps, ensure_ascii=False)) + '" data-context="' + esc(namespace) + '" data-definition-sha256="' + capability_identity(data, nodes, records, key) + '"><summary>' + esc(node.get('title', key)) + ' · ' + esc(key) + '</summary><p>分组：' + esc(group_for(key)) + '</p>')
        add('<p class="local-gaps">本 M：' + esc(capability_gaps(detail, findings, key)) + '</p>')
        if model:
            add('<dl><dt>Topic（主题）</dt><dd>' + esc(node.get('topic', '未提供')) + '</dd><dt>Scope（范围）</dt><dd>' + ''.join('<h4>' + esc(label) + '</h4>' + items(values) for label, values in scope_sections(node)) + '</dd><dt>Purpose（目的）</dt><dd>' + esc(node.get('purpose', '未提供')) + '</dd></dl>')
            for mapping in detail['projections']:
                if key in mapping['tco_node_ids']:
                    add('<p>KYM 追溯：' + esc(mapping['kym_node_id']) + ' → ' + esc(mapping['relation']) + '；理由：' + esc(mapping['rationale']) + '</p>')
        else:
            def source_tree(branch):
                texts = [statements[r]['text'] + ' · ' + statements[r].get('support_status', '未提供') for r in branch.get('statement_refs', []) if r in statements]
                return '<details open><summary>' + esc(branch.get('title', branch['id'])) + '</summary>' + (items(texts) if texts else '') + ''.join(source_tree(child) for child in branch.get('children', [])) + '</details>'
            add('<h3>来源业务规则、TSP 子树与支持状态</h3>' + source_tree(node))
        add('<h3>候选 Coverage 原始状态</h3>')
        record = records.get(key)
        if record:
            fields = [f for f in ['coverage_state', 'evidence_peak_state', 'effective_state', 'claim_state', 'capability_lifecycle_state', 'validation_posture', 'resolution_status', 'claim_ceiling'] if f in record]
            add('<p>仅显示来源提供的状态字段，不补造缺失轴，不合并为单一通过标记。</p><dl>' + ''.join('<dt>' + esc(f) + '</dt><dd>' + esc(record[f]) + '</dd>' for f in fields) + '</dl>')
            if 'scope_coverage' in record or 'missing_scope_items' in record:
                add('<h4>旧版 Scope Coverage（原始候选记录，不转换为 v4 requirement 或迁移状态）</h4><div class="table-scroll"><table><thead><tr><th>Scope 原文</th><th>候选覆盖状态</th><th>原始说明</th></tr></thead><tbody>' + ''.join('<tr><td>' + esc(row['scope']) + '</td><td>' + esc(row['coverage_state']) + '</td><td>' + esc(row['notes']) + '</td></tr>' for row in record.get('scope_coverage', [])) + '</tbody></table></div>')
                add('<h4>旧版 missing_scope_items（原始缺失范围）</h4>' + (items(record['missing_scope_items']) if record.get('missing_scope_items') else '<p>来源记录未列缺失范围；不能据此批准覆盖。</p>'))
        else:
            add('<p class="notice">not_evaluated（未评估）：未提供本 M 的 Coverage；KYM 不赋覆盖状态。</p>')
        if not cap:
            add('<p class="notice">详细内容缺失；未评估，不能推导已覆盖。</p></details>')
            return
        add('<h3>输入 Input</h3>' + items(cap['inputs']) + '<h3>流程图（点击步骤跳至 Process）</h3><div class="flow">')
        for step in cap['process']:
            add('<div class="flow-step"><a href="#' + anchor(view_key + '/' + step['id']) + '">' + esc(step['label']) + '</a>')
            for edge in step['transitions']:
                label = next((s['label'] for s in cap['process'] if s['id'] == edge['to']), edge['to'])
                add('<div class="edge">' + esc(edge['condition']) + ' → <a href="#' + anchor(view_key + '/' + edge['to']) + '">' + esc(label) + '</a></div>')
            add('</div>')
        add('</div><h3>处理 Process</h3><ol>')
        for step in cap['process']:
            add('<li id="' + anchor(view_key + '/' + step['id']) + '"><strong>' + esc(step['label']) + '</strong><p>' + esc(step['description']) + '</p>' + (items([e['condition'] + ' → ' + e['to'] for e in step['transitions']]) if step['transitions'] else '<p>终止步骤，无后继转移。</p>') + '</li>')
        add('</ol><h3>输出 Output</h3>' + items(cap['outputs']) + '<h3>代表例子</h3>' + items(cap['examples']) + ''.join(diagram_html(d) for d in cap['diagrams']))
        add('<h3>本 M 逐行为判断</h3><p>主要下一步分类不覆盖全部轴；结合适用边界与证据说明保留其他同时存在的缺口。建议动作不产生调度、测试义务或状态提升。</p><div class="table-scroll"><table><thead><tr><th>应证明的行为 / requirement</th><th>已有证据与适用边界</th><th>主要缺口</th><th>最小建议动作 / 验收判据</th></tr></thead><tbody>')
        for row in cap['assessment']:
            refs = ['<a href="' + esc(links[r]) + '">' + esc(r) + '</a>' if r in links else esc(r) for r in row['evidence_refs']]
            requirement_states = {r.get('requirement_id'): r.get('coverage_state', 'not_evaluated') for r in records.get(key, {}).get('requirement_coverage', [])}
            row_states = '；'.join(req + ': ' + str(requirement_states.get(req, 'not_evaluated（未评估）')) for req in row['requirement_ids']) or 'unmapped（未映射）'
            add('<tr><td><strong>' + esc(row['behavior']) + '</strong><p>' + esc(', '.join(row['requirement_ids']) or 'unmapped（未映射，不能视为已覆盖）') + '</p></td><td>' + esc(row['evidence_summary']) + '<p>适用边界：' + esc(row['applicability']) + '</p><p>候选逐 requirement 状态：' + esc(row_states) + '</p>' + '<br>'.join(refs) + '</td><td>' + esc(GAPS[row['gap_kind']]) + '</td><td>' + esc(row['next_action']) + '<p>验收：' + esc(row['acceptance']) + '</p></td></tr>')
        add('</tbody></table></div><h3>本 M 业务问题</h3>' + items(cap['questions']) + '</details>')
    for profile in profiles:
        add('<details class="profile" data-profile="' + esc(profile['id']) + '" open><summary>' + esc(profile['label']) + '</summary><p>' + esc(profile.get('objective', profile.get('description', ''))) + '</p><p>声明上限：' + esc(profile.get('claim_ceiling', '未提供')) + '</p>')
        groups = {}
        for key in membership[profile['id']]:
            groups.setdefault(group_for(key), []).append(key)
        for label, keys in groups.items():
            add('<details class="mc-group" open><summary>' + esc(label) + '</summary>')
            for key in keys:
                render_capability(key, 'tp/' + profile['id'] + '/mc/' + label, [profile['id']])
            add('</details>')
        add('<p>同目标其他 M 的缺口见各自条目；全局问题见独立区域。</p></details>')
    unassigned = sorted(caps.keys() - {key for keys in membership.values() for key in keys})
    if unassigned:
        add('</section><section><h2>尚未归入目标的能力</h2>')
        for key in unassigned:
            render_capability(key, 'unassigned', [])
    add('</section><section id="global-questions"><h2>全局问题（不归入任一 TP/M）</h2>' + items(detail['global_questions'] + [q['summary'] for q in kym.get('unresolved_questions', []) if q.get('status') == 'open']) + '</section><section><h2>建议目标切片（候选，未写入 canonical TP）</h2>')
    for p in intake['proposed_profiles']:
        add('<article><h3>' + esc(p['label']) + '</h3><p>' + esc(p['objective']) + '</p><p>推荐理由：' + esc(p['rationale']) + '</p></article>')
    add('</section><section><h2>来源与模板摘要</h2><p>' + esc(VERSION) + '；模板摘要：' + esc(report['template_sha256']) + '</p><ul>')
    for key, binding in detail['bindings'].items():
        add('<li><a href="' + esc(links[binding['path']]) + '">' + esc(key + ': ' + binding['path']) + '</a> · sha256:' + esc(binding['sha256']) + '</li>')
    add('</ul><p>详细伴随：<a href="sources/reader-detail.json">reader-detail.json</a>；检查：<a href="reader-report.json">reader-report.json</a>。</p></section></main><script>' + js + '</script></body></html>')
    return ''.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'render'])
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--kym', required=True, help='工作区相对路径')
    parser.add_argument('--model', help='省略为仅 KYM；提供则为 TCO')
    parser.add_argument('--coverage')
    parser.add_argument('--detail', required=True)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        workspace, paths, data = load_inputs(args)
        audited = audit(workspace, paths, data)
        findings = audited[0]
        evidence_bytes = {ref: path.read_bytes() for ref, path in audited[3].items()}
        assets = [SKILL / 'assets/reader.css', SKILL / 'assets/reader.js', Path(__file__).resolve()]
        report = {'schema': 'kym_tco.reader_report.v1', 'candidate_only': True, 'reader_version': VERSION,
                  'template_sha256': digest(b''.join(p.read_bytes() for p in assets)),
                  'error_count': sum(f['severity'] == 'error' for f in findings),
                  'warning_count': sum(f['severity'] == 'warning' for f in findings), 'findings': findings,
                  'input_sha256': {k: digest(payload) for k, payload in data['_input_bytes'].items()},
                  'evidence_sha256': {k: digest(payload) for k, payload in evidence_bytes.items()},
                  'authority_note': '伴随检查不替代候选包验证，不创建批准、覆盖接受态或运行保证。'}
        if args.command == 'render':
            if args.output_dir is None:
                raise ValueError('render 必须提供 --output-dir')
            output = (workspace / args.output_dir).resolve()
            if output.exists() and (not output.is_dir() or any(output.iterdir())):
                raise ValueError('输出目录必须不存在或为空，避免覆盖既有内容')
            if any(p == output or output in p.parents for p in paths.values()):
                raise ValueError('输出目录不得包含输入来源')
            if any(f['code'] == 'UNSAFE_LINK' for f in findings):
                raise ValueError('发现不安全证据链接，拒绝渲染')
            links, copies = {}, {}
            for key, binding in data['detail']['bindings'].items():
                rel = 'sources/inputs/' + binding['path']
                if paths[key].suffix.lower() in {'.html', '.htm', '.svg', '.js', '.xml', '.xhtml'}:
                    rel += '.txt'
                links[binding['path']], copies[rel] = quote(rel, safe='/'), data['_input_bytes'][key]
            for ref, path in audited[3].items():
                rel = 'sources/evidence/' + ref
                if path.suffix.lower() in {'.html', '.htm', '.svg', '.js', '.xml', '.xhtml'}:
                    rel += '.txt'
                links[ref], copies[rel] = quote(rel, safe='/'), evidence_bytes[ref]
            page = render_page(data, report, audited, links)
            markdown = markdown_page(data, report, audited, links)
            output.mkdir(parents=True, exist_ok=True)
            for rel, payload in copies.items():
                target = output / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            (output / 'sources/reader-detail.json').write_bytes(data['_input_bytes']['detail'])
            (output / 'reader.html').write_text(page, encoding='utf-8')
            (output / 'reader.md').write_text(markdown, encoding='utf-8')
            (output / 'reader-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report['error_count'] else 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(json.dumps({'candidate_only': True, 'error_count': 1, 'findings': [{'code': 'READER_INPUT_ERROR', 'severity': 'error', 'message': str(exc)}], 'authority_note': '输入错误不创建接受态。'}, ensure_ascii=False, indent=2))
        return 2


if __name__ == '__main__':
    sys.exit(main())
