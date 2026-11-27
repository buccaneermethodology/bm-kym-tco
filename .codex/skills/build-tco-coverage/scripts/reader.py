#!/usr/bin/env python3
"""只读、离线、可搬移的 KYM/TCO 候选阅读伴随工具。"""
from __future__ import annotations
import argparse
import base64
import hashlib
import html
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import sys
import types
from urllib.parse import quote, unquote, urlsplit

VERSION = 'reader-v1.1.0'
SKILL = Path(__file__).resolve().parents[1]
GAPS = {'implementation': '实现缺口', 'verification': '验证缺口', 'version_applicability': '版本适用性缺口', 'evidence_binding': '证据绑定缺口', 'business_question': '业务待确认', 'not_evaluated': '未评估', 'none': '未报告主要缺口（不是批准）'}


_FULL = types.ModuleType('_reader_full')
_FULL.__file__ = str(SKILL / 'scripts/reader_full.py')
exec(compile(Path(_FULL.__file__).read_bytes(), _FULL.__file__, 'exec'), _FULL.__dict__)

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


def load_ecq(workspace, paths, data, assessment):
    if 'model' not in data:
        raise ValueError('KYM-only 不接受 --ecq；E/C/Q 仅用于 TCO 能力阅读')
    assessment_path = local_path(workspace, assessment)
    engine_path = local_path(SKILL, 'scripts/ecq.py', must_exist=False)
    if not engine_path.is_file():
        raise ValueError('本 Skill 未携带 ECQ 引擎；请使用同包 build-tco-coverage 的 reader，不从全局 Skill 补载')
    module = types.ModuleType('_reader_ecq')
    module.__file__ = str(engine_path)
    try:
        exec(compile(engine_path.read_bytes(), str(engine_path), 'exec'), module.__dict__)
    except ImportError as exc:
        raise ValueError('可选 ECQ 路径缺少已声明依赖：' + exc.name) from exc
    projection = module.evaluate(workspace, assessment)
    if not isinstance(projection, dict) or projection.get('schema') != 'ecq.projection.v1' or projection.get('lifecycle') != 'candidate_not_approved':
        raise ValueError('ECQ 返回的投影 schema 或候选边界不匹配')
    if projection.get('product_id') != data['model']['product_id']:
        raise ValueError('ECQ 与 reader 产品不一致')
    consumed = {}
    for binding in projection['bindings']:
        base, value = binding.get('base'), binding.get('path')
        if base not in {'workspace', 'skill'}:
            raise ValueError('ECQ binding 必须显式声明 workspace 或 skill namespace')
        target = local_path(workspace if base == 'workspace' else SKILL, value)
        payload = target.read_bytes()
        if digest(payload) != binding.get('sha256'):
            raise ValueError('ECQ 消费来源摘要漂移：' + base + '/' + value)
        identity = (base, value)
        if identity in consumed and consumed[identity] != payload:
            raise ValueError('ECQ 重复来源绑定冲突：' + value)
        consumed[identity] = payload
    mandatory = [('skill', 'scripts/ecq.py'), ('skill', 'references/ecq-assessment.schema.json'), ('workspace', assessment)]
    if any(identity not in consumed for identity in mandatory):
        raise ValueError('ECQ 未绑定当前引擎、schema 或 assessment 原字节')
    if consumed[('workspace', assessment)] != assessment_path.read_bytes():
        raise ValueError('ECQ assessment 在计算后漂移')
    for context in projection.get('source_contexts', []):
        binding = context['assessment']
        if binding.get('base') != 'workspace' or ('workspace', binding.get('path')) not in consumed or digest(consumed[('workspace', binding['path'])]) != binding.get('sha256'):
            raise ValueError('ECQ 快照来源上下文没有绑定正确 assessment 原字节')
    sources = {source['id']: source for source in projection['sources']}
    for kind, field in [('model', 'current_model_source_id'), ('coverage', 'current_coverage_source_id')]:
        source_id = projection['scope'].get(field)
        source = sources.get(source_id, {})
        if kind not in data:
            disconnected = (kind == 'coverage' and source.get('kind') == 'coverage'
                            and source.get('availability') == 'not_connected' and source.get('binding') is None)
            if source_id is not None and not disconnected:
                raise ValueError('ECQ 绑定了 reader 未提供的当前 ' + kind)
            continue
        binding = source.get('binding') or {}
        identity = ('workspace', binding.get('path'))
        if source.get('kind') != kind or identity not in consumed or consumed[identity] != data['_input_bytes'][kind]:
            raise ValueError('ECQ 当前 ' + kind + ' 与 reader 实际输入原字节不一致')
    data['_ecq'] = projection
    data['_ecq_bytes'] = consumed
    data['_ecq_assessment'] = assessment


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
    result = findings, nodes, capabilities, evidence_paths, records, tree, statements
    _FULL.audit(data, result, data.get('_require_full', False))
    return result


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
    gaps += [('尚未做覆盖评估' if r['gap_kind'] == 'not_evaluated' else GAPS[r['gap_kind']]) for r in detail['capabilities'].get(key, {}).get('assessment', []) if r['gap_kind'] != 'none']
    return '；'.join(dict.fromkeys(gaps)) or '未报告主要缺口（仍须核对证据适用性）'


def coverage_note(data, records, key):
    if 'model' not in data:
        return '本页只解释 KYM 业务规则，不评定 TCO 能力的覆盖状态。'
    coverage = data.get('coverage')
    if '_ecq' in data:
        if coverage is None:
            return '原 Coverage 文件未提供；E/C/Q 使用另行绑定的资料，不补写原 Coverage。'
        if coverage.get('coverage_mode') == 'not_evaluated':
            return '原 Coverage 文件标记为 not_evaluated（未评估）；这只说明原文件，E/C/Q 的独立已知资料仍保留。'
        if key not in records:
            return '原 Coverage 文件没有本 M 的记录；E/C/Q 另按其绑定资料呈现，不替原文件补造记录。'
        return ''
    if coverage is None:
        return '本次未提供 Coverage 文件，尚不能判断本 M 已实现、运行或验证到哪一步。'
    if coverage.get('coverage_mode') == 'not_evaluated':
        return '本次 Coverage 文件明确标记为 not_evaluated（未评估）；尚不能判断本 M 已实现、运行或验证到哪一步。'
    if key not in records:
        return '已提供 Coverage 文件，但缺少本 M 的评估记录；本 M 的当前进展未知，不能据此认定未实现或已覆盖。'
    return ''


def assessment_context(data, records, key, rows):
    """只提取同一 M 内逐字相同、出现多次的说明，不跨 M 合并。"""
    shared = {}
    for field in ('evidence_summary', 'applicability'):
        values = [row[field] for row in rows]
        label = '共用证据说明' if field == 'evidence_summary' else '共用适用边界'
        shared[field] = {value: label + str(index + 1) for index, value in enumerate(dict.fromkeys(value for value in values if values.count(value) > 1))}
    evaluated = 'coverage' in data and data['coverage'].get('coverage_mode') != 'not_evaluated'
    states = {r.get('requirement_id'): r.get('coverage_state', 'not_evaluated') for r in records.get(key, {}).get('requirement_coverage', [])}
    return shared, evaluated, states


def assessment_mapping(row, evaluated, states):
    if not row['requirement_ids']:
        return '缺少要求对应，不能视为已覆盖。' if evaluated else ''
    if not evaluated:
        return '要求对应（尚未评估）：' + '；'.join(req + ('（候选原值：' + str(states[req]) + '）' if req in states else '') for req in row['requirement_ids'])
    return '候选逐要求状态：' + '；'.join(req + ': ' + str(states.get(req, '缺少本要求的评估记录')) for req in row['requirement_ids'])


def rule_mapping(tree, projection):
    source = projection['kym_node_id']
    title = tree.get(source, {}).get('title', '来源业务规则待确认')
    relation = {'direct': '直接对应', 'split': '拆分对应', 'merge': '合并对应'}[projection['relation']]
    return '对应 KYM 业务规则：' + title + '（' + relation + '）'


def source_text(payload, name):
    if payload.startswith((b'%PDF-', b'%!PS', b'PK\x03\x04', b'GIF87a', b'GIF89a', b'\x89PNG', b'\xff\xd8\xff', b'RIFF', b'\x7fELF')):
        return None
    mime = mimetypes.guess_type(name)[0]
    if mime and not (mime.startswith('text/') or mime in {'application/json', 'application/xml', 'application/javascript', 'image/svg+xml'} or mime.endswith('+json') or mime.endswith('+xml')):
        return None
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None
    if any(ord(ch) < 32 and ch not in '\r\n\t' for ch in text):
        return None
    return text


def source_page(name, text, raw_relative, payload):
    """固定风格的纯文本阅读页；源内容只进入转义后的 pre。"""
    css = (SKILL / 'assets/reader.css').read_text(encoding='utf-8')
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>' + esc(name) + ' · 来源阅读</title><style>' + css + '</style></head><body><header><h1>来源阅读</h1><p>' + esc(name) + '</p></header><main><section>'
            '<p>以下按纯文本展示，保留原内容；阅读页不执行来源中的代码。</p><p>原文件摘要：sha256:' + digest(payload) + '</p><p><a href="' + esc(raw_relative) + '" download>下载原文件（保留原始字节）</a></p>'
            '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + esc(text) + '</pre></section></main></body></html>')


ECQ_LABELS = {
    'acceptance_blocked': '有验收阻断', 'insufficient_evidence': '证据不足',
    'pending_verification': '待验证', 'no_blocker_observed': '本范围未发现阻断',
    'observed_pass': '实际观察通过', 'reported_pass': '仅报告通过', 'failed': '失败',
    'blocked': '受阻', 'not_run': '未运行', 'unknown': '未知',
    'not_evaluated': '未评估', 'met': '已达目标', 'gaps': '仍有缺口', 'stale': '原证据当前不适用',
    'design': '设计', 'implementation': '实现', 'verification': '验证', 'integration': '集成',
    'evidence': '补证据', 'done': '已完成', 'doing': '进行中',
    'completed': '已完成', 'forward': '推进', 'regression': '回归', 'rework': '返工',
    'design_only': '已有设计', 'code_bound': '已有代码绑定', 'test_bound': '已有测试绑定',
    'bounded_runtime': '有限范围真实运行', 'contract_defined': '合同已定义',
    'implemented': '已实现', 'maintained': '持续维护', 'producer_checked': '作者已检查',
    'independently_validated': '已经独立验证',
    'complete': '完整', 'partial': '部分', 'connected': '已接入', 'unavailable': '未接入',
}


def ecq_value(value):
    if value is None:
        return '未知'
    if value is True:
        return '是'
    if value is False:
        return '否'
    if isinstance(value, (dict, list)):
        return _FULL.text(value)
    return str(value)


def ecq_label(value):
    return ECQ_LABELS.get(value, value) if isinstance(value, str) else ecq_value(value)


def ecq_title(value, fallback):
    if isinstance(value, dict):
        return value.get('title') or value.get('text') or value.get('label') or fallback
    return value if isinstance(value, str) else fallback


def ecq_c_text(metric):
    return metric.get('display', {}).get('text', '未知')


def ecq_q_text(metric):
    observed = metric.get('observed')
    if observed is not None and observed[1] == 0:
        return '本范围尚无可计数的关键验收要求，不输出验证比例'
    return (' / '.join(str(v) for v in observed) + ' 条关键要求已获适用观察验证' if observed is not None else '关键要求验证情况未知')


def ecq_writer(markdown):
    """两种阅读输出使用同一业务投影与同一段落/表格顺序。"""
    out = []
    fmt = md_text if markdown else esc
    def p(value):
        out.append(fmt(value) + '\n' if markdown else '<p>' + fmt(value) + '</p>')
    def heading(value, level=4):
        out.append('#' * level + ' ' + fmt(value) + '\n' if markdown else '<h' + str(level) + '>' + fmt(value) + '</h' + str(level) + '>')
    def table(headers, rows, role=None, row_ids=None):
        if markdown:
            out.append('| ' + ' | '.join(fmt(v) for v in headers) + ' |\n| ' + ' | '.join('---' for _ in headers) + ' |')
            out.extend('| ' + ' | '.join(fmt(v) for v in row) + ' |' for row in rows)
            out.append('')
        else:
            out.append('<div class="table-scroll"><table' + (' data-ecq-table="' + esc(role) + '"' if role else '') + '><thead><tr>' + ''.join('<th>' + fmt(v) + '</th>' for v in headers) + '</tr></thead><tbody>' + ''.join('<tr' + (' data-requirement-id="' + esc(row_ids[index]) + '"' if row_ids is not None else '') + '>' + ''.join('<td>' + fmt(v) + '</td>' for v in row) + '</tr>' for index, row in enumerate(rows)) + '</tbody></table></div>')
    def details(title, value):
        out.append('<details><summary>' + esc(title) + '</summary>')
        def rows(item, prefix=''):
            if isinstance(item, dict):
                return [row for k, v in item.items() for row in rows(v, prefix + (' / ' if prefix else '') + str(k))]
            if isinstance(item, list):
                return [row for i, v in enumerate(item) for row in rows(v, prefix + ' · ' + str(i + 1))] or [[prefix, '未列条目']]
            return [[prefix, ecq_value(item)]]
        table(['字段', '来源值'], rows(value))
        out.append('</details>\n')
    def link(label, href):
        out.append('[' + fmt(label) + '](' + href + ')\n' if markdown else '<p><a href="' + esc(href) + '">' + esc(label) + '</a></p>')
    return out, p, heading, table, details, link


def ecq_reference(projection, ref, link, links, label='查看来源', snapshot_id=None):
    if not isinstance(ref, dict) or not ref.get('source_id'):
        return
    snapshot_id = snapshot_id or projection['snapshot_id']
    context = next((c for c in projection.get('source_contexts', []) if c['snapshot_id'] == snapshot_id), None)
    sources = context['sources'] if context else (projection['sources'] if snapshot_id == projection['snapshot_id'] else [])
    source = next((s for s in sources if s['id'] == ref['source_id']), None)
    binding = (source or {}).get('binding') or {}
    href = links.get(('ecq', 'workspace', binding.get('path')))
    if href:
        link(label + ' · ' + ref['source_id'] + ' ' + ref.get('pointer', ''), href)


def ecq_capability(data, key, links, markdown=False, tps=(), historical=False):
    out, p, heading, table, details, link = ecq_writer(markdown)
    projection = data.get('_ecq')
    cap = next((c for c in (projection or {}).get('capabilities', []) if c['m1_id'] == key), None)
    if projection:
        scope = projection['scope']
        p('E/C/Q 统计范围：' + '、'.join(scope['tp_ids']) + '；截止：' + projection['cutoff'] + '。切换目标筛选不会重新计算。')
        if tps and not set(tps).issubset(scope['tp_ids']):
            p('当前展示目标未纳入这份 E/C/Q 统计范围；以下仅供查看原统计范围资料，不能作为当前目标达成结论。')
    if not markdown:
        out.append('<div class="ecq-cards">')
    for axis, title in [('E', 'E · 已做工作'), ('C', 'C · 功能实现覆盖'), ('Q', 'Q · 验证与问题')]:
        if not markdown:
            out.append('<article class="ecq-card" data-ecq-axis="' + axis + '">')
        heading(title)
        if not cap:
            p({'E': '真实 Session 与贡献尚未整理，数量未知。', 'C': '功能实现程度未知，尚无本范围的可信估算。', 'Q': '关键要求验证及问题资料尚未整理。'}[axis])
        elif axis == 'E':
            metric = cap['E']
            p(('实际参与的 Session 数：' + str(metric['count'])) if metric['count'] is not None else '真实 Session 历史未完整接入，数量未知；不把计划当成执行。')
            p('Session 历史完整性：' + ecq_label(metric.get('history_completeness', 'unknown')) + '；部分或未知时，上述数量只代表已接入的已知贡献。')
            p('已完成：' + ecq_value(metric.get('completed_session_count')) + '；进行中：' + ecq_value(metric.get('doing_session_count')) + '；完成分类未知：' + ecq_value(metric.get('classification_unknown_count')))
            p('贡献事件：' + ecq_value(metric.get('event_count')) + '；同一 Session 的多项贡献不会增加 Session 数。')
            p('贡献结果：' + '；'.join(ecq_label(name) + ' ' + str(count) for name, count in metric['outcome_counts'].items()))
        elif axis == 'C':
            metric = cap['C']
            p('当前 C 不适用：本 M 已移出当前范围。' if historical else ecq_c_text(metric))
            p(metric['scope_label'] + '；原业务要求数：' + ecq_value(metric['requirement_count']) + '；权重分母：' + ecq_value(metric['denominator_weight']) + '；未知要求：' + ecq_value(metric['unknown_count']))
            core_text = metric['display'].get('core_text')
            if core_text and core_text != ecq_c_text(metric):
                p(core_text)
            elif not core_text:
                p('核心行为：' + {'available': '已具备', 'unavailable': '未具备', 'unknown': '待确认'}.get(metric['core_behavior'], '待确认'))
            if not metric['whole_M_claim_allowed']:
                p('只描述已整理范围，不能代表整个 M。')
            details('C 数值区间与完整口径', {'区间原显示': metric['display']['detail_text'], '原始指标': metric})
        else:
            metric = cap['Q']
            p(('历史质量资料，不作当前验收结论。' if historical else '') + ecq_q_text(metric))
            p(ecq_label(metric['status']) + '；问题数：' + ecq_value(metric.get('issue_count')) + '。这是逐要求判断，不是综合质量分。')
        if not markdown:
            out.append('</article>')
    if not markdown:
        out.append('</div>')
    if not cap:
        p('尚未接入本 M 的 E/C/Q 评估；不从原 Coverage 状态推算比例或质量。')
        return '\n'.join(out)
    heading('实际 Session 与贡献时间线')
    table(['Session', '工作类别', '当前分类', '原记录'], [[s['session_id'], ecq_label(s.get('category')), ecq_label(s.get('classification')), ecq_value(s.get('source_values'))] for s in cap['E']['sessions']])
    for session in cap['E']['sessions']:
        ecq_reference(projection, session.get('record_ref'), link, links, 'Session 原记录')
    table(['Session', '贡献时间', '发生了什么', '结果'], [[event['session_id'], event['event'].get('observed_at', '未提供'), event['event'].get('summary', '未提供'), ecq_label(event['event'].get('outcome', 'unknown'))] for event in cap['E']['events']])
    details('贡献事件、分类与技术来源详情', cap['E'])
    heading('质量问题与主要缺口')
    for issue in cap['Q'].get('issues', []):
        p(_FULL.text(issue))
    seen_gaps = set()
    for condition in cap['Q'].get('conditions', []):
        gap_text = _FULL.text(condition['gaps']) if condition.get('gaps') else ''
        if gap_text and gap_text not in seen_gaps:
            seen_gaps.add(gap_text)
            p('待补证据（相同缺口共用此说明）：' + gap_text)
    heading('推进前建议先做什么')
    if not cap['next_step_ids']:
        p('本次没有登记本 M 的候选动作；不自动创建或执行 Session。')
    for step_id in cap['next_step_ids']:
        step = next(step for step in projection['next_steps'] if step['id'] == step_id)
        link(('候选 Session：' if step['kind'] == 'session' else '最小动作：') + step['title'], '#' + anchor('ecq-step/' + step_id))
    return '\n'.join(out)


def ecq_overview(data, links, markdown=False):
    projection = data.get('_ecq')
    if not projection:
        return ''
    out, p, heading, table, details, link = ecq_writer(markdown)
    heading('E/C/Q 资料范围与历史', 2)
    p('这份评估是候选阅读资料，不创建业务批准。截止：' + projection['cutoff'] + '；统计目标：' + '、'.join(projection['scope']['tp_ids']))
    p('全范围实际 Session：' + ecq_value(projection['global']['E']['count']) + '；同一 Session 跨 M 只在全范围计一次。')
    heading('来源已有的未执行计划：不计入已做工作', 3)
    table(['计划 Session', '原工作类别', '原计划状态'], [[s['session_id'], ecq_label(s.get('category')), ecq_label(s['source_values'].get('status'))] for s in projection['global']['E'].get('planned_sessions', [])])
    for session in projection['global']['E'].get('planned_sessions', []):
        ecq_reference(projection, session.get('record_ref'), link, links, '未执行计划原记录')
    p('全范围 C：' + ecq_c_text(projection['global']['C']) + '；权重分母：' + ecq_value(projection['global']['C']['denominator_weight']))
    target = projection.get('tp_target', 'unknown')
    p('目标门槛是否达成：' + (ecq_label(target) if isinstance(target, str) else '见绑定目标报告原文') + '。应查看目标报告，不能从 C 功能份额推定目标达成。')
    table(['资料', '是否接入', '完整性', '抽取可信'], [[s['id'], ecq_label(s['availability']), ecq_label(s['completeness']), ecq_value(s.get('trusted'))] for s in projection['sources']])
    details('范围、版本与警告详情', {'scope': projection['scope'], 'warnings': projection['warnings']})
    for warning in projection['warnings']:
        p('来源提醒：' + warning['message'])
    table(['历史快照', '截止', '当时的 M', '全范围 C', '全范围实际 Session'], [[h['snapshot_id'], h['cutoff'], '、'.join(h['current_m1_ids']), ecq_c_text(h['global_C']), ecq_value(h['global_E']['count'])] for h in projection['history']])
    if isinstance(target, dict):
        table(['目标', '原报告状态'], [[pid, ecq_label(row.get('status', 'unknown'))] for pid, row in target.items()])
    for history in projection['history']:
        link('查看历史快照及其当时来源 · ' + history['snapshot_id'], '#' + anchor('ecq-context/' + history['snapshot_id']))
    for report in projection.get('source_validation_reports', []):
        heading('原工具检查报告：保留原结论', 3)
        original = report['report'] if isinstance(report['report'], dict) else {}
        summary = []
        for key, label in [('status', '检查状态'), ('result', '检查结果'), ('exit_code', '退出码')]:
            if key in original and not isinstance(original[key], (dict, list)):
                value = original[key]
                displayed = str(ecq_label(value))
                if isinstance(value, str) and displayed != value:
                    displayed += ' (' + value + ')'
                summary.append([label, displayed])
        if summary:
            table(['原报告项目', '原报告值'], summary)
        p('完整结论、逐项结果和技术字段保留在原报告中；本页只读展示，不创建新的运行或批准。')
        binding = report.get('binding') or {}
        href = links.get(('ecq', 'workspace', binding.get('path')))
        if href:
            link('查看原工具报告原文', href)
    historical = [c for c in projection['capabilities'] if not c['current']]
    if historical:
        heading('历史 M：不进入当前唯一能力与 C 分母', 2)
        for cap in historical:
            heading(ecq_title(cap.get('definition'), cap['m1_id']), 3)
            out.append(ecq_capability(data, cap['m1_id'], links, markdown, historical=True))
    heading('候选下一步：共享卡只保存一份', 2)
    p('先核对和复用已有证据；这些是建议，不触发执行、不进入 E，也不承诺 C 增长。')
    for step in projection['next_steps']:
        out.append('<a id="' + anchor('ecq-step/' + step['id']) + '"></a>')
        heading(('候选 Session：' if step['kind'] == 'session' else '最小动作：') + step['title'], 3)
        p('建议理由：' + step['reason'])
        for label, field in [('预期产出', 'expected_outputs'), ('怎样算完成', 'acceptance_criteria'), ('依赖候选', 'dependencies'), ('外部阻断', 'blockers')]:
            p(label + '：' + ('；'.join(step[field]) or '未登记'))
        p('投入估算：' + ecq_value(step['effort']))
        for ref in step['input_refs']:
            ecq_reference(projection, ref, link, links, '建议依据')
        details('共享候选的技术对应', {'id': step['id'], 'mapping_ids': step['mapping_ids']})
    heading('E/C/Q 来源、规则与核对材料', 2)
    for context in projection.get('source_contexts', []):
        out.append('<a id="' + anchor('ecq-context/' + context['snapshot_id']) + '"></a>')
        out.append('<details><summary>' + esc('来源快照 · ' + context['snapshot_id']) + '</summary>')
        binding = context['assessment']
        link('该快照的评估伴随原文', links[('ecq', binding['base'], binding['path'])])
        for source in context['sources']:
            binding = source.get('binding') or {}
            href = links.get(('ecq', 'workspace', binding.get('path')))
            if href:
                link('该快照来源 · ' + source['id'], href)
        out.append('</details>\n')
    for binding in projection['bindings']:
        href = links[('ecq', binding['base'], binding['path'])]
        link(binding['base'] + '/' + binding['path'], href)
        p('sha256:' + binding['sha256'])
    link('评估伴随原文', links[('ecq', 'workspace', data['_ecq_assessment'])])
    link('引擎投影原文', links[('generated', 'ecq')])
    return '\n'.join(out)


def target_contract(profile, markdown=False):
    out, p, heading, table, details, _ = ecq_writer(markdown)
    out.append('<details><summary>本目标的门槛与原字段详情</summary>')
    p('目标门槛与功能实现份额 C 分开；更严的运行目标不代表功能实现倒退，也不代表目标已达成。')
    goals = {'contract': '合同目标', 'implementation': '实现目标', 'runtime': '运行目标'}
    table(['目标', '覆盖门槛', '生命周期门槛', '验证门槛'], [[goals.get(g['id'], g['id']), ecq_label(g.get('minimum_coverage_state', '未设置')), ecq_label(g.get('minimum_lifecycle_state', '未设置')), ecq_label(g.get('required_validation_posture', '未设置'))] for g in profile.get('coverage_goals', [])])
    details('目标原始要求与适用范围', profile)
    out.append('</details>\n')
    return '\n'.join(out)


def markdown_page(data, report, audited, links):
    findings, nodes, caps, _, records, tree, statements = audited
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
    add(_FULL.readings(data, markdown=True, links=links))
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
        add(_FULL.readings(data, markdown=True, links=links, select=('model' if model else 'kym', key)))
        if model:
            add(ecq_capability(data, key, links, markdown=True, tps=[namespace.split('/')[1]] if namespace.startswith('tp/') else []))
        if model:
            add('**Topic（主题）：** ' + md_text(_FULL.text(node.get('topic', '未提供'))) + '\n')
            add('**Scope（范围）：**\n')
            for label, values in scope_sections(node):
                add(label + '：\n')
                bullets(values)
            add('**Purpose（目的）：** ' + md_text(_FULL.text(node.get('purpose', '未提供'))) + '\n')
            for projection in detail['projections']:
                if key in projection['tco_node_ids']:
                    add(md_text(rule_mapping(tree, projection)) + '\n')
                    add('<details><summary>对应关系详情</summary>\n')
                    add('来源节点 ID：' + md_text(projection['kym_node_id']) + '；关系原值：' + md_text(projection['relation']) + '；说明：' + md_text(projection['rationale']) + '\n')
                    add('</details>\n')
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
        if model:
            add('<details><summary>原 Coverage 状态与字段详情</summary>\n')
        add('#' * (level + 1) + ' 候选 Coverage 原始状态\n')
        note = coverage_note(data, records, key)
        if note:
            add(md_text(note) + '\n')
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
        if model:
            add('</details>\n')
        cap = detail['capabilities'].get(key)
        if not cap:
            add('详细内容缺失；未评估，不能推导已覆盖。\n')
            return
        add('#' * (level + 1) + ' 输入 Input\n')
        bullets(cap['inputs'])
        add('#' * (level + 1) + ' 流程图与详细步骤\n')
        add(_FULL.flow(cap, view_key, markdown=True))
        add('#' * (level + 1) + ' 输出 Output\n')
        bullets(cap['outputs'])
        add('#' * (level + 1) + ' 代表例子\n')
        bullets(cap['examples'])
        for diagram in cap['diagrams']:
            add(('（模型图）' if diagram['kind'] == 'modeling' else '（界面区块图）') + diagram_html(diagram))
        add('#' * (level + 1) + ' 本 M 逐行为判断\n')
        add('下方建议先说明最优先的一步；其余待满足条件仍列在状态与证据中。怎样算完成，见每行验收条件。\n')
        shared, evaluated, states = assessment_context(data, records, key, cap['assessment'])
        if not evaluated:
            add('本次尚未进行逐要求覆盖评估，不能据此认定已覆盖。下表保留各行为的证据、建议与验收条件。\n')
        for field, values in shared.items():
            for value, label in values.items():
                add('**' + label + '：** ' + md_text(value) + '\n')
        add('| 应证明的行为 / requirement | 已有证据与适用边界 | 主要缺口 | 最小建议动作 / 验收判据 |\n| --- | --- | --- | --- |')
        for row in cap['assessment']:
            mapping = assessment_mapping(row, evaluated, states)
            refs = ['[' + md_text(ref) + '](' + links[ref] + ')' if ref in links else md_text(ref) for ref in row['evidence_refs']]
            evidence = shared['evidence_summary'].get(row['evidence_summary'])
            applicability = shared['applicability'].get(row['applicability'])
            add('| ' + md_text(row['behavior']) + ('<br>' + md_text(mapping) if mapping else '') + ' | ' + md_text('见' + evidence if evidence else row['evidence_summary']) + '<br>适用边界：' + md_text('见' + applicability if applicability else row['applicability']) + '<br>' + '<br>'.join(refs) + ' | ' + md_text(GAPS[row['gap_kind']]) + ' | ' + md_text(row['next_action']) + '<br>验收：' + md_text(row['acceptance']) + ' |')
        add('')
        add('#' * (level + 1) + ' 本 M 业务问题\n')
        bullets(cap['questions'], '本 M 未记录业务问题。')
    for profile in profiles:
        add('### ' + md_text(profile['label']) + '\n')
        add(md_text(profile.get('objective', profile.get('description', ''))) + '\n')
        add('声明上限：' + md_text(profile.get('claim_ceiling', '未提供')) + '\n')
        if model:
            add(target_contract(profile, markdown=True))
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
    add(ecq_overview(data, links, markdown=True))
    add('## 建议目标切片（候选，未写入 canonical TP）\n')
    for profile in detail['intake']['proposed_profiles']:
        add('### ' + md_text(profile['label']) + '\n')
        add(md_text(profile['objective']) + '\n')
        add('推荐理由：' + md_text(profile['rationale']) + '\n')
    add('## 来源与模板摘要\n')
    add(md_text(VERSION) + '；模板摘要：' + report['template_sha256'] + '\n')
    for key, binding in detail['bindings'].items():
        add('- [' + md_text(key + ': ' + binding['path']) + '](' + links[binding['path']] + ') · sha256:' + binding['sha256'])
    add('\n详细伴随：[reader-detail.json](' + links[('generated', 'detail')] + ')；检查：[reader-report.json](' + links[('generated', 'report')] + ')。\n')
    return '\n'.join(out)


def render_page(data, report, audited, links):
    findings, nodes, caps, _, records, tree, statements = audited
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
    add(_FULL.readings(data, links=links))
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
        add(_FULL.readings(data, links=links, select=('model' if model else 'kym', key)))
        add('<p class="local-gaps">本 M：' + esc(capability_gaps(detail, findings, key)) + '</p>')
        if model:
            add(ecq_capability(data, key, links, tps=tps))
        if model:
            add('<dl><dt>Topic（主题）</dt><dd>' + esc(_FULL.text(node.get('topic', '未提供'))) + '</dd><dt>Scope（范围）</dt><dd>' + ''.join('<h4>' + esc(label) + '</h4>' + items(values) for label, values in scope_sections(node)) + '</dd><dt>Purpose（目的）</dt><dd>' + esc(_FULL.text(node.get('purpose', '未提供'))) + '</dd></dl>')
            for mapping in detail['projections']:
                if key in mapping['tco_node_ids']:
                    add('<p>' + esc(rule_mapping(tree, mapping)) + '</p><details><summary>对应关系详情</summary><p>来源节点 ID：' + esc(mapping['kym_node_id']) + '；关系原值：' + esc(mapping['relation']) + '；说明：' + esc(mapping['rationale']) + '</p></details>')
        else:
            def source_tree(branch):
                texts = [statements[r]['text'] + ' · ' + statements[r].get('support_status', '未提供') for r in branch.get('statement_refs', []) if r in statements]
                return '<details open><summary>' + esc(branch.get('title', branch['id'])) + '</summary>' + (items(texts) if texts else '') + ''.join(source_tree(child) for child in branch.get('children', [])) + '</details>'
            add('<h3>来源业务规则、TSP 子树与支持状态</h3>' + source_tree(node))
        if model:
            add('<details><summary>原 Coverage 状态与字段详情</summary>')
        add('<h3>候选 Coverage 原始状态</h3>')
        note = coverage_note(data, records, key)
        if note:
            add('<p class="notice">' + esc(note) + '</p>')
        record = records.get(key)
        if record:
            fields = [f for f in ['coverage_state', 'evidence_peak_state', 'effective_state', 'claim_state', 'capability_lifecycle_state', 'validation_posture', 'resolution_status', 'claim_ceiling'] if f in record]
            add('<p>仅显示来源提供的状态字段，不补造缺失轴，不合并为单一通过标记。</p><dl>' + ''.join('<dt>' + esc(f) + '</dt><dd>' + esc(record[f]) + '</dd>' for f in fields) + '</dl>')
            if 'scope_coverage' in record or 'missing_scope_items' in record:
                add('<h4>旧版 Scope Coverage（原始候选记录，不转换为 v4 requirement 或迁移状态）</h4><div class="table-scroll"><table><thead><tr><th>Scope 原文</th><th>候选覆盖状态</th><th>原始说明</th></tr></thead><tbody>' + ''.join('<tr><td>' + esc(row['scope']) + '</td><td>' + esc(row['coverage_state']) + '</td><td>' + esc(row['notes']) + '</td></tr>' for row in record.get('scope_coverage', [])) + '</tbody></table></div>')
                add('<h4>旧版 missing_scope_items（原始缺失范围）</h4>' + (items(record['missing_scope_items']) if record.get('missing_scope_items') else '<p>来源记录未列缺失范围；不能据此批准覆盖。</p>'))
        if model:
            add('</details>')
        if not cap:
            add('<p class="notice">详细内容缺失；未评估，不能推导已覆盖。</p></details>')
            return
        add('<h3>输入 Input</h3>' + items(cap['inputs']) + '<h3>流程图与详细步骤</h3>')
        add(_FULL.flow(cap, view_key))
        add('<h3>输出 Output</h3>' + items(cap['outputs']) + '<h3>代表例子</h3>' + items(cap['examples']) + ''.join(diagram_html(d) for d in cap['diagrams']))
        add('<h3>本 M 逐行为判断</h3><p>下方建议先说明最优先的一步；其余待满足条件仍列在状态与证据中。怎样算完成，见每行验收条件。</p>')
        shared, evaluated, states = assessment_context(data, records, key, cap['assessment'])
        if not evaluated:
            add('<p>本次尚未进行逐要求覆盖评估，不能据此认定已覆盖。下表保留各行为的证据、建议与验收条件。</p>')
        for field, values in shared.items():
            for value, label in values.items():
                add('<p><strong>' + esc(label) + '：</strong>' + esc(value) + '</p>')
        add('<div class="table-scroll"><table><thead><tr><th>应证明的行为 / requirement</th><th>已有证据与适用边界</th><th>主要缺口</th><th>最小建议动作 / 验收判据</th></tr></thead><tbody>')
        for row in cap['assessment']:
            refs = ['<a href="' + esc(links[r]) + '">' + esc(r) + '</a>' if r in links else esc(r) for r in row['evidence_refs']]
            mapping = assessment_mapping(row, evaluated, states)
            evidence = shared['evidence_summary'].get(row['evidence_summary'])
            applicability = shared['applicability'].get(row['applicability'])
            add('<tr><td><strong>' + esc(row['behavior']) + '</strong>' + ('<p>' + esc(mapping) + '</p>' if mapping else '') + '</td><td>' + esc('见' + evidence if evidence else row['evidence_summary']) + '<p>适用边界：' + esc('见' + applicability if applicability else row['applicability']) + '</p>' + '<br>'.join(refs) + '</td><td>' + esc(GAPS[row['gap_kind']]) + '</td><td>' + esc(row['next_action']) + '<p>验收：' + esc(row['acceptance']) + '</p></td></tr>')
        add('</tbody></table></div><h3>本 M 业务问题</h3>' + items(cap['questions']) + '</details>')
    for profile in profiles:
        add('<details class="profile" data-profile="' + esc(profile['id']) + '" open><summary>' + esc(profile['label']) + '</summary><p>' + esc(profile.get('objective', profile.get('description', ''))) + '</p><p>声明上限：' + esc(profile.get('claim_ceiling', '未提供')) + '</p>')
        if model:
            add(target_contract(profile))
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
    if '_ecq' in data:
        add('</section><section class="ecq-overview">' + ecq_overview(data, links))
    add('</section><section><h2>来源与模板摘要</h2><p>' + esc(VERSION) + '；模板摘要：' + esc(report['template_sha256']) + '</p><ul>')
    for key, binding in detail['bindings'].items():
        add('<li><a href="' + esc(links[binding['path']]) + '">' + esc(key + ': ' + binding['path']) + '</a> · sha256:' + esc(binding['sha256']) + '</li>')
    add('</ul><p>详细伴随：<a href="' + esc(links[('generated', 'detail')]) + '">reader-detail.json</a>；检查：<a href="' + esc(links[('generated', 'report')]) + '">reader-report.json</a>。</p></section></main><script>' + js + '</script></body></html>')
    return ''.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'render'])
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--kym', required=True, help='工作区相对路径')
    parser.add_argument('--model', help='省略为仅 KYM；提供则为 TCO')
    parser.add_argument('--coverage')
    parser.add_argument('--ecq', help='可选 ECQ assessment；工作区相对路径，需当前 Skill 随包引擎')
    parser.add_argument('--detail', required=True)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--require-full', action='store_true', help='要求完整阅读 profile 和全量结构门禁')
    args = parser.parse_args(argv)
    try:
        workspace, paths, data = load_inputs(args)
        if args.ecq:
            load_ecq(workspace, paths, data, args.ecq)
        data['_require_full'] = args.require_full
        audited = audit(workspace, paths, data)
        findings = audited[0]
        evidence_bytes = {ref: path.read_bytes() for ref, path in audited[3].items()}
        assets = [SKILL / 'assets/reader.css', SKILL / 'assets/reader.js', Path(__file__).resolve(), SKILL / 'scripts/reader_full.py', SKILL / 'references/reader-detail.schema.json']
        report = {'schema': 'kym_tco.reader_report.v1', 'candidate_only': True, 'reader_version': VERSION,
                  'template_sha256': digest(b''.join(p.read_bytes() for p in assets)),
                  'error_count': sum(f['severity'] == 'error' for f in findings),
                  'warning_count': sum(f['severity'] == 'warning' for f in findings), 'findings': findings,
                  'input_sha256': {k: digest(payload) for k, payload in data['_input_bytes'].items()},
                  'evidence_sha256': {k: digest(payload) for k, payload in evidence_bytes.items()},
                  'authority_note': '伴随检查不替代候选包验证，不创建批准、覆盖接受态或运行保证。'}
        if '_ecq' in data:
            report['ecq'] = {'schema': data['_ecq']['schema'], 'snapshot_id': data['_ecq']['snapshot_id'], 'cutoff': data['_ecq']['cutoff'], 'scope': data['_ecq']['scope'], 'bindings': data['_ecq']['bindings']}
        report['reading_profile'] = data['detail'].get('reading_profile', 'legacy')
        report['full_detail_checked'] = data['detail'].get('reading_profile') == _FULL.PROFILE and not report['error_count']
        report['semantic_review'] = 'not_observed'
        if args.command == 'render':
            if report['error_count'] and (args.require_full or data['detail'].get('reading_profile') == _FULL.PROFILE):
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
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
            def add_source(key, rel, payload, name):
                if rel in copies and copies[rel] != payload:
                    raise ValueError('来源复制路径冲突：' + rel)
                copies[rel] = payload
                text = source_text(payload, name)
                if text is None:
                    links[key] = quote(rel, safe='/')
                else:
                    reader_rel = 'readable/' + digest(rel.encode('utf-8')) + '.html'
                    copies[reader_rel] = source_page(name, text, '../' + quote(rel, safe='/'), payload).encode('utf-8')
                    links[key] = quote(reader_rel, safe='/')
            for key, binding in data['detail']['bindings'].items():
                rel = 'sources/inputs/' + binding['path']
                if paths[key].suffix.lower() in {'.html', '.htm', '.svg', '.js', '.xml', '.xhtml'}:
                    rel += '.txt'
                add_source(binding['path'], rel, data['_input_bytes'][key], binding['path'])
            for ref, path in audited[3].items():
                rel = 'sources/evidence/' + ref
                if path.suffix.lower() in {'.html', '.htm', '.svg', '.js', '.xml', '.xhtml'}:
                    rel += '.txt'
                add_source(ref, rel, evidence_bytes[ref], ref)
            add_source(('generated', 'detail'), 'sources/reader-detail.json', data['_input_bytes']['detail'], 'reader-detail.json')
            if '_ecq' in data:
                for (base, value), payload in data['_ecq_bytes'].items():
                    root = workspace if base == 'workspace' else SKILL
                    if local_path(root, value).read_bytes() != payload:
                        raise ValueError('ECQ 来源在复制前漂移：' + base + '/' + value)
                    rel = 'sources/ecq/' + base + '/' + value
                    if Path(value).suffix.lower() in {'.html', '.htm', '.svg', '.js', '.xml', '.xhtml'}:
                        rel += '.txt'
                    add_source(('ecq', base, value), rel, payload, base + '/' + value)
                projection_bytes = (json.dumps(data['_ecq'], ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
                add_source(('generated', 'ecq'), 'sources/ecq-projection.json', projection_bytes, 'ecq-projection.json')
            report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            add_source(('generated', 'report'), 'reader-report.json', report_bytes, 'reader-report.json')
            page = render_page(data, report, audited, links)
            markdown = markdown_page(data, report, audited, links)
            profiles, membership, group_for = reader_structure(data, audited[1], audited[2])
            views = [(key, 'tp/' + profile['id'] + '/mc/' + group_for(key)) for profile in profiles for key in membership[profile['id']]]
            assigned = {key for key, namespace in views}
            views.extend((key, 'unassigned') for key in sorted(audited[2].keys() - assigned))
            for key, namespace in views:
                cap = data['detail']['capabilities'].get(key)
                if cap:
                    view = namespace + '/' + key
                    copies['diagrams/' + anchor(view) + '.svg'] = _FULL.graph(cap, view, '../reader.html').encode('utf-8')
            output.mkdir(parents=True, exist_ok=True)
            for rel, payload in copies.items():
                target = output / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            (output / 'reader.html').write_text(page, encoding='utf-8')
            (output / 'reader.md').write_text(markdown, encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report['error_count'] else 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        message = str(exc).replace(str(args.workspace.resolve()), '[workspace]').replace(str(SKILL), '[skill]')
        print(json.dumps({'candidate_only': True, 'error_count': 1, 'findings': [{'code': getattr(exc, 'code', 'READER_INPUT_ERROR'), 'severity': 'error', 'message': message}], 'authority_note': '输入错误不创建接受态。'}, ensure_ascii=False, indent=2))
        return 2


if __name__ == '__main__':
    sys.exit(main())
