#!/usr/bin/env python3
"""只读校验候选 TCO 进展；生成可核对的离线投影。Python 3.9+ 标准库。"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from datetime import datetime
from urllib.parse import quote

SCHEMA = None
STATES = ('unknown', 'not_started', 'in_progress', 'partial', 'blocked', 'completed')
NOTICE = 'candidate_not_approved（候选，未经批准）；推进项完成不代表产品完成；总体 Coverage 仅投影原 CLI 报告。'


class Invalid(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_digest(value):
    return 'sha256:' + digest(canonical(value))


def load(path):
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result, 'JSON duplicate key: ' + key)
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(Invalid('JSON 非有限数值: ' + x)))


def shape(value, spec, where='$'):
    if '$ref' in spec:
        return shape(value, SCHEMA['$defs'][spec['$ref'].split('/')[-1]], where)
    if 'anyOf' in spec:
        for option in spec['anyOf']:
            try:
                shape(value, option, where)
                return
            except Invalid:
                pass
        raise Invalid(where + ': 不符合 nullable 字段结构')
    if 'const' in spec:
        need(value == spec['const'], where + ': const 不匹配')
    if 'enum' in spec:
        need(value in spec['enum'], where + ': enum 无效')
    kind = spec.get('type')
    if kind == 'null':
        need(value is None, where + ': 必须 null')
    elif kind == 'object':
        need(isinstance(value, dict), where + ': 必须 object')
        need(set(value) == set(spec['required']), where + ': 缺字段或多余字段')
        for key, child in spec['properties'].items():
            shape(value[key], child, where + '.' + key)
    elif kind == 'array':
        need(isinstance(value, list), where + ': 必须 array')
        need(len(value) >= spec.get('minItems', 0), where + ': 数量不足')
        if spec.get('uniqueItems'):
            need(len({canonical(x) for x in value}) == len(value), where + ': 重复值')
        for index, child in enumerate(value):
            shape(child, spec['items'], where + '[' + str(index) + ']')
    elif kind == 'string':
        need(isinstance(value, str), where + ': 必须 string')
        need(len(value.strip()) >= spec.get('minLength', 0), where + ': 不得空白')
        if 'pattern' in spec:
            need(re.fullmatch(spec['pattern'], value) is not None, where + ': pattern 无效')


def timepoint(value):
    need(isinstance(value, str) and 'T' in value, '时间必须为带时区 ISO datetime')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise Invalid('无效 ISO 时间: ' + value)
    need(dt.tzinfo is not None and dt.utcoffset() is not None, '时间缺少时区: ' + value)
    return dt


def local_path(workspace, value):
    need(isinstance(value, str) and value.strip(), '路径为空')
    relative = Path(value)
    need(not relative.is_absolute(), '输入路径必须相对 workspace: ' + value)
    resolved = (workspace / relative).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise Invalid('路径或 symlink 越过 workspace: ' + value)
    need(resolved.is_file(), '输入文件不存在: ' + value)
    return resolved


def reference(workspace, ref):
    path = local_path(workspace, ref['path'])
    need(digest(path.read_bytes()) == ref['sha256'], '字节摘要已漂移: ' + ref['path'])
    return path


def by_id(rows, label):
    need(isinstance(rows, list), label + ' 必须 array')
    result = {}
    for row in rows:
        need(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'].strip(), label + ' 缺 id')
        need(row['id'] not in result, label + ' 重复 id: ' + row['id'])
        result[row['id']] = row
    return result


def evidence(workspace, refs, required=True):
    need(not required or bool(refs), '缺少真实证据引用')
    for ref in refs:
        reference(workspace, ref)


def side_data(workspace, side, product_id):
    result = {}
    for key in ('model', 'coverage', 'report'):
        result[key] = None if side[key] is None else load(reference(workspace, side[key]))
    model, coverage, report = result['model'], result['coverage'], result['report']
    need(isinstance(model, dict) and model.get('schema') in ['kym_tco.tco_model.v' + str(x) for x in (2, 3, 4)], '不支持的 model schema')
    need(isinstance(coverage, dict) and coverage.get('schema') in ['kym_tco.tco_coverage_model.v' + str(x) for x in (2, 3)], '不支持的 Coverage schema')
    need(model.get('product_id') == coverage.get('product_id') == product_id, 'model/Coverage product identity 不一致')
    need(model.get('candidate_status') == coverage.get('candidate_status') == 'candidate_not_approved', '仅接受 candidate model/Coverage')
    need(coverage.get('source_tco_model_digest') == json_digest(model), 'Coverage source_tco_model_digest 不匹配')
    result['nodes'] = by_id(model.get('nodes'), 'model.nodes')
    result['profiles'] = by_id(model.get('target_profiles'), 'model.target_profiles')
    result['m1_ids'] = {key for key, node in result['nodes'].items() if node.get('node_kind') == 'm1'}
    for profile in result['profiles'].values():
        for req in by_id(profile.get('requirements'), 'Profile.requirements').values():
            need(isinstance(req.get('node_ids'), list) and set(req['node_ids']) <= set(result['nodes']), 'Profile 关联不存在节点')
    if report is not None:
        need(isinstance(report, dict) and report.get('schema') in ('kym_tco.tco_coverage_report.v1', 'kym_tco.tco_coverage_report.v2', 'kym_tco.tco_coverage_report.v3'), '不支持的 candidate report schema')
        need(report.get('report_mode') == 'candidate_not_approved' and report.get('product_id') == product_id, 'report authority/product 不匹配')
        need(report.get('source_tco_model_digest') == json_digest(model), 'report source_tco_model_digest 不匹配')
        need(report.get('source_coverage_digest') == json_digest(coverage), 'report source_coverage_digest 不匹配')
        need(report.get('coverage_mode') == coverage.get('coverage_mode') and report.get('coverage_mode') in ('repo', 'not_evaluated'), 'report Coverage mode 不匹配')
        need(set(by_id(report.get('nodes'), 'report.nodes')) == set(result['nodes']), 'report nodes 分母不匹配')
        profiles = by_id(report.get('target_profiles'), 'report.target_profiles')
        need(set(profiles) == set(result['profiles']), 'report Profile 分母不匹配')
        for pid, profile in profiles.items():
            source = result['profiles'][pid]
            if report['schema'] == 'kym_tco.tco_coverage_report.v1':
                expected = ('not_evaluated',) if report['coverage_mode'] == 'not_evaluated' else ('met', 'gaps')
                need(profile.get('status') in expected, 'report v1 status 无效')
                need(profile.get('requirement_count') == len(source['requirements']), 'report v1 requirement 分母不匹配')
                if report['coverage_mode'] != 'not_evaluated':
                    need(set(by_id(profile.get('requirements'), 'report v1 requirements')) == set(by_id(source['requirements'], 'model.requirements')), 'report v1 requirement 身份不匹配')
                continue
            goals = by_id(profile.get('coverage_goals'), 'report.coverage_goals')
            need(set(goals) == set(by_id(source.get('coverage_goals'), 'model.coverage_goals')), 'report goal 分母不匹配')
            for goal in goals.values():
                need(set(by_id(goal.get('requirements'), 'report.requirements')) == set(by_id(source['requirements'], 'model.requirements')), 'report requirement 分母不匹配')
                expected = ('not_evaluated',) if report['coverage_mode'] == 'not_evaluated' else ('Met', 'Gap')
                need(goal.get('status') in expected, 'report goal status 无效')
    return result


def validate(workspace, path, explicit_base=None, chain=None):
    chain = [] if chain is None else chain
    need(path not in chain, 'base_progress 循环')
    doc = load(path)
    shape(doc, SCHEMA)
    cutoff = timepoint(doc['cutoff'])
    included, excluded = set(doc['included_stages']), set(doc['excluded_stages'])
    need(len(excluded) == len(doc['excluded_stages']), 'excluded_stages 重复')
    need(not included & excluded, '纳入和排除阶段有交集')
    base_ref = doc['base_progress']
    base_path = reference(workspace, base_ref) if base_ref else None
    if explicit_base is not None:
        need(base_path is not None and explicit_base == base_path, '--base-progress 与文档引用不一致')
    base = validate(workspace, base_path, chain=chain + [path]) if base_path else None
    before = side_data(workspace, doc['before'], doc['product_id'])
    after = side_data(workspace, doc['after'], doc['product_id'])
    m1_ids = before['m1_ids'] | after['m1_ids']
    items = by_id(doc['items'], 'items')
    requirements = by_id(doc['requirements'], 'requirements')
    if base:
        old = base['doc']
        need(doc['product_id'] == old['product_id'], 'base product 不匹配')
        need(doc['snapshot_id'] not in base['snapshot_ids'], 'snapshot_id 与祖先重复')
        need(cutoff > timepoint(old['cutoff']), 'cutoff 未严格递增')
        need(all(doc['before'][k] == old['after'][k] for k in ('model', 'coverage')), 'base.after -> current.before 绑定断链')
        need(set(old['included_stages']) <= included, '累计 included_stages 丢失')
        need(set(base['items']) <= set(items), '基线推进项被删除')
        need(set(base['requirements']) <= set(requirements), '基线原子要求被删除')
    changes = {}
    for change in doc['changes']:
        key = (change['kind'], change['id'])
        need(key not in changes, '重复 change')
        changes[key] = change
        evidence(workspace, change['evidence_refs'])
    actual = set()
    for kind, collection in (('node', 'nodes'), ('profile', 'profiles')):
        a, b = before[collection], after[collection]
        for key in set(a) | set(b):
            if a.get(key) != b.get(key):
                actual.add((kind, key))
    need(set(changes) == actual, 'model/Profile 变化未逐项处置或 changes 有多余条目')
    for rid, req in requirements.items():
        need(set(req['m1_ids']) <= m1_ids, '要求 M1 不存在: ' + rid)
        evidence(workspace, req['evidence_refs'], required=req['disposition'] != 'retained')
        if req['disposition'] != 'retained':
            need(req['reason'].strip(), '要求处置缺少理由: ' + rid)
        previous = base['requirements'].get(rid) if base else None
        if previous:
            changed = any(req[k] != previous[k] for k in ('text', 'm1_ids'))
            need(not changed or req['disposition'] == 'revised', '要求文本/归属变化必须 revised: ' + rid)
            need(req['disposition'] == previous['disposition'] or req['disposition'] != 'retained', '要求处置不可静默恢复 retained')
        elif base is None:
            need(req['disposition'] == 'retained', '初始要求必须 retained')
    new_events = []
    for iid, item in items.items():
        need(set(item['m1_ids']) <= m1_ids, '推进项 M1 不存在: ' + iid)
        need(set(item['requirement_ids']) <= set(requirements), '推进项关联不存在原子要求: ' + iid)
        linked_m1 = set()
        for rid in item['requirement_ids']:
            linked_m1.update(requirements[rid]['m1_ids'])
        need(set(item['m1_ids']) <= linked_m1, '共享推进项缺少匹配的要求关联: ' + iid)
        previous = base['items'].get(iid) if base else None
        prefix = len(previous['events']) if previous else 0
        if previous:
            need(all(item[k] == previous[k] for k in ('m1_ids', 'requirement_ids', 'title', 'acceptance')), '推进项关联语义被改写: ' + iid)
            need(item['events'][:prefix] == previous['events'], '历史 event 前缀被修改: ' + iid)
        state, last = 'unknown', None
        for index, event in enumerate(item['events']):
            observed = timepoint(event['observed_at'])
            need(event['before'] == state, '事件 before/after 断链: ' + iid)
            need(last is None or observed > last, '事件时间未严格递增: ' + iid)
            need(observed <= cutoff, '事件晚于 cutoff')
            need(event['stage'] in included and event['stage'] not in excluded, '事件阶段未纳入或被排除')
            evidence(workspace, event['evidence_refs'])
            if event['outcome'] in ('no_change', 'evidence_only'):
                need(event['before'] == event['after'], 'no_change/evidence_only 不得修改状态')
            if event['outcome'] == 'forward':
                need(event['after'] != event['before'] and event['after'] not in ('unknown', 'blocked'), 'forward 必须记录明确推进后的非未知、非阻塞状态')
            if event['outcome'] == 'regression':
                need(event['after'] != event['before'], 'regression 必须记录状态变化')
            if index >= prefix:
                need(event['task_id'] in doc['tasks'], '新增 event 不属于本次 tasks')
                need(not base or observed > timepoint(base['doc']['cutoff']), '新增 event 不晚于基线 cutoff')
                new_events.append({'item_id': iid, 'm1_ids': item['m1_ids'], **event})
            state, last = event['after'], observed
    need(set(doc['tasks']) == {event['task_id'] for event in new_events}, '本次 task 缺贡献事件；无推进须写 no_change 解释')
    return {'doc': doc, 'before': before, 'after': after, 'items': items, 'requirements': requirements,
            'new_events': new_events, 'snapshot_ids': (base['snapshot_ids'] if base else []) + [doc['snapshot_id']],
            'progress_sha256': digest(path.read_bytes())}


def compact(value):
    if value == 'not_evaluated':
        return 'N/A（未评估）'
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def rendering(result, workspace, output):
    doc = result['doc']
    esc = lambda x: html.escape(str(x), quote=True)
    def link(ref, mode='html'):
        target = reference(workspace, ref)
        url = quote(os.path.relpath(target, output).replace(os.sep, '/'), safe='/')
        return '<a href="' + esc(url) + '">' + esc(ref['path']) + '</a>' if mode == 'html' else '[' + md(ref['path']) + '](' + url + ')'
    def md(value):
        return re.sub(r'([\\`*_\[\]])', r'\\\1', html.escape(compact(value), quote=False)).replace('|', '\\|').replace('\n', '<br>')
    def table(headers, rows):
        markdown.extend(['', '| ' + ' | '.join(headers) + ' |', '|' + '|'.join('---' for _ in headers) + '|'])
        markdown.extend('| ' + ' | '.join(md(x) for x in row) + ' |' for row in rows)
        return '<div class="table-wrap"><table><thead><tr>' + ''.join('<th>' + esc(x) + '</th>' for x in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + esc(compact(x)) + '</td>' for x in row) + '</tr>' for row in rows) + '</tbody></table></div>'
    sections = []
    markdown = ['# TCO 任务贡献与阶段进展', '', NOTICE, '', '截止点：' + md(doc['cutoff']),
                '纳入阶段：' + md(', '.join(doc['included_stages'])), '排除阶段：' + md(', '.join(doc['excluded_stages'])),
                '本次任务：' + md(', '.join(doc['tasks'])), '共享推进项去重总数：' + str(len(doc['items'])), '']
    sections.append('<p>' + esc(NOTICE) + '</p><p>截止点：' + esc(doc['cutoff']) + '；纳入阶段：' + esc(', '.join(doc['included_stages'])) + '；排除阶段：' + esc(', '.join(doc['excluded_stages'])) + '</p>')
    legend = '状态图例：unknown 未观察历史；not_started 有证据表明未开始；in_progress 正在推进；partial 局部完成；blocked 存在阻塞；completed 仅此推进项达到验收描述。'
    markdown += [legend, '整体结果图例：Met 原 CLI 目标满足；Gap 原 CLI 仍有缺口；N/A 未评估；unknown 未绑定报告。', '']
    sections.append('<p>' + esc(legend) + '</p><p>整体结果：Met 原 CLI 目标满足；Gap 仍有缺口；N/A 未评估；unknown 未绑定报告。</p>')
    for side in ('before', 'after'):
        data = result[side]
        binding = doc[side]
        label = '之前' if side == 'before' else '之后'
        markdown += ['## ' + label + '能力与整体结果', '', 'M1 分母：' + str(len(data['m1_ids'])) + '；Profile 分母：' + str(len(data['profiles'])), '']
        parts = ['<h2>' + label + '能力与整体结果</h2><p>M1 分母：' + str(len(data['m1_ids'])) + '；Profile 分母：' + str(len(data['profiles'])) + '</p>']
        for key in ('model', 'coverage', 'report'):
            if binding[key] is not None:
                markdown.append(key + '：' + link(binding[key], 'md') + ' SHA256 ' + binding[key]['sha256'])
                parts.append('<p>' + key + '：' + link(binding[key]) + ' SHA256 ' + esc(binding[key]['sha256']) + '</p>')
        report = data['report']
        if report is None:
            markdown += ['', '整体 Coverage：unknown（未知；未绑定原 CLI 报告）', '']
            parts.append('<p>整体 Coverage：unknown（未知；未绑定原 CLI 报告）</p>')
            profile_rows = [(pid, 'unknown', 'unknown', 'unknown', 'unknown') for pid in data['profiles']]
        else:
            markdown += ['', '整体 Coverage 取自原 CLI；not_evaluated = N/A（未评估）。', '']
            parts.append('<p>整体 Coverage 取自原 CLI；not_evaluated = N/A（未评估）。</p>')
            profile_rows = []
            for profile in report['target_profiles']:
                for goal in profile.get('coverage_goals', [profile]):
                    profile_rows.append((profile['id'], goal.get('id', 'legacy'), compact(goal.get('status', 'unknown')), goal.get('met_count'), goal.get('gap_count')))
        parts.append('<h3>目标整体结果</h3>' + table(['Profile', '目标轴', '原状态', 'met_count' if report else '原满足数未知', 'gap_count' if report else '原缺口数未知'], profile_rows))
        node_rows = by_id(report['nodes'], 'report.nodes') if report else {}
        axes = []
        for nid in sorted(data['m1_ids']):
            row = node_rows.get(nid, {})
            cov = row.get('coverage') or {}
            axes.append((nid, cov.get('capability_lifecycle_state', row.get('capability_lifecycle_state', 'unknown')),
                         cov.get('validation_posture', row.get('validation_posture', 'unknown')),
                         row.get('direct_state', row.get('direct_coverage_state', cov.get('coverage_state', 'unknown'))),
                         row.get('effective_state', row.get('coverage_state', 'unknown')),
                         row.get('claim_state', 'unknown')))
        parts.append('<h3>M1 原始能力轴</h3>' + table(['M1', 'lifecycle', 'validation', 'direct', 'effective', 'claim'], axes))
        requirement_rows = []
        if report:
            for profile in report['target_profiles']:
                for goal in profile.get('coverage_goals', [profile]):
                    for req in goal.get('requirements', []):
                        required = {k: v for k, v in req.items() if k.startswith(('minimum_', 'required_')) or k == 'target_coverage_state'}
                        observed = {k: v for k, v in req.items() if k.startswith(('current_', 'effective_', 'claim_')) or k in ('status', 'met')}
                        gaps = {k: v for k, v in req.items() if 'gap' in k or k.startswith('missing_')}
                        requirement_rows.append((profile['id'], goal.get('id', 'legacy'), req['id'], required or 'unknown', observed or 'unknown', gaps or '未提供缺口字段'))
        if requirement_rows:
            parts.append('<h3>原要求阈值、观察与缺口</h3>' + table(['Profile', '目标轴', '要求', 'required 原值', 'effective/观察原值', '缺口原值'], requirement_rows))
        if report:
            parts.append('<details><summary>展开绑定的原 CLI 报告字段</summary><pre>' + esc(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre></details>')
        sections.append(''.join(parts))
    markdown += ['## 推进项、阻塞与下一步', '', '| ID | 标题 | M1 | 验收 | 状态 | owner | 下一步 |', '|---|---|---|---|---|---|---|']
    cards = []
    history_markdown = []
    for item in doc['items']:
        state = item['events'][-1]['after'] if item['events'] else 'unknown'
        markdown.append('| ' + ' | '.join(md(x) for x in (item['id'], item['title'], item['m1_ids'], item['acceptance'], state, item['owner'], item['next_step'])) + ' |')
        attributes = {'m1': item['m1_ids'], 'task': sorted({e['task_id'] for e in item['events']}), 'stage': sorted({e['stage'] for e in item['events']}),
                      'events': [{'task': e['task_id'], 'stage': e['stage']} for e in item['events']]}
        attrs = ' '.join('data-' + k + '="' + esc(json.dumps(v, ensure_ascii=False)) + '"' for k, v in attributes.items())
        if item['events']:
            last_event = item['events'][-1]
            history = '最新历史贡献：' + last_event['task_id'] + ' / ' + last_event['stage'] + ' / ' + last_event['observed_at'] + '；' + last_event['summary']
            history_markdown += ['', '- ' + md(item['id']) + ' ' + md(history)] + ['  ' + link(ref, 'md') + ' SHA256 ' + ref['sha256'] for ref in last_event['evidence_refs']]
            history_html = '<p>' + esc(history) + '</p>' + ''.join('<p>' + link(ref) + ' SHA256 ' + esc(ref['sha256']) + '</p>' for ref in last_event['evidence_refs'])
        else:
            history_markdown += ['', '- ' + md(item['id']) + '：unknown（未观察到历史事件和证据）']
            history_html = '<p>unknown（未观察到历史事件和证据）</p>'
        cards.append('<article class="progress-item" ' + attrs + '><h3>' + esc(item['id'] + ' · ' + item['title']) + '</h3><p>M1：' + esc(', '.join(item['m1_ids'])) + '；状态：' + esc(state) + '；owner：' + esc(item['owner']) + '</p><p>验收：' + esc(item['acceptance']) + '</p><p>来源要求：' + esc(', '.join(item['requirement_ids'])) + '</p><p>下一步：' + esc(item['next_step']) + '</p>' + history_html + '</article>')
    markdown += ['', '### 各推进项的最新历史证据', ''] + history_markdown
    sections.append('<h2>推进项、阻塞与下一步</h2><p>可见推进项：<span id="visible-item-count">' + str(len(cards)) + '</span>（共享项去重）</p>' + ''.join(cards))
    markdown += ['', '## 本次任务贡献', '']
    event_html = []
    for event in result['new_events']:
        text = event['task_id'] + ' / ' + event['stage'] + ' / ' + event['item_id'] + '：' + event['before'] + ' → ' + event['after'] + '；' + event['outcome'] + '；' + event['summary']
        markdown += ['- ' + md(text) + '（' + event['observed_at'] + '）']
        markdown += ['  ' + link(ref, 'md') + ' SHA256 ' + ref['sha256'] for ref in event['evidence_refs']]
        attrs = ' '.join('data-' + k + '="' + esc(json.dumps(v, ensure_ascii=False)) + '"' for k, v in {'m1': event['m1_ids'], 'task': [event['task_id']], 'stage': [event['stage']], 'events': [{'task': event['task_id'], 'stage': event['stage']}]}.items())
        event_html.append('<article class="contribution" ' + attrs + '><p>' + esc(text) + '（' + esc(event['observed_at']) + '）</p>' + ''.join('<p>' + link(ref) + ' SHA256 ' + esc(ref['sha256']) + '</p>' for ref in event['evidence_refs']) + '</article>')
    sections.append('<h2>本次任务贡献</h2>' + ''.join(event_html))
    markdown += ['', '## 原子要求处置', '']
    req_html = []
    for req in doc['requirements']:
        text = req['id'] + ' / ' + ', '.join(req['m1_ids']) + ' / ' + req['disposition'] + '：' + req['text'] + '；理由：' + req['reason']
        markdown += ['- ' + md(text)] + ['  ' + link(ref, 'md') for ref in req['evidence_refs']]
        req_html.append('<p>' + esc(text) + ' ' + ' '.join(link(ref) for ref in req['evidence_refs']) + '</p>')
    sections.append('<h2>原子要求处置</h2>' + ''.join(req_html))
    markdown += ['', '## 模型与目标变更处置', '']
    change_html = []
    for change in doc['changes']:
        text = change['kind'] + ' / ' + change['id'] + '：' + change['reason']
        markdown += ['- ' + md(text)] + ['  ' + link(ref, 'md') for ref in change['evidence_refs']]
        change_html.append('<p>' + esc(text) + ' ' + ' '.join(link(ref) for ref in change['evidence_refs']) + '</p>')
    if not doc['changes']:
        markdown += ['无模型或目标合同变化。']
        change_html.append('<p>无模型或目标合同变化。</p>')
    sections.append('<h2>模型与目标变更处置</h2>' + ''.join(change_html))
    sections.insert(0, '<p>筛选说明：推进项按全部历史事件匹配任务和阶段；本次贡献仅显示新增事件。任务与阶段组合须命中同一事件，历史命中不计为本次新增贡献。</p>')
    filters = []
    all_tasks = sorted(set(doc['tasks']) | {e['task_id'] for item in doc['items'] for e in item['events']})
    for key, label, values in [('m1', 'M1', sorted(result['before']['m1_ids'] | result['after']['m1_ids'])), ('task', '任务（含历史）', all_tasks), ('stage', '阶段', doc['included_stages'])]:
        filters.append('<label>' + label + ' <select id="filter-' + key + '"><option value="">全部</option>' + ''.join('<option value="' + esc(v) + '">' + esc(v) + '</option>' for v in values) + '</select></label>')
    javascript = '''function applyFilters(){
const m1=document.getElementById('filter-m1').value;
const task=document.getElementById('filter-task').value;
const stage=document.getElementById('filter-stage').value;
document.querySelectorAll('.progress-item,.contribution').forEach(el=>{
const matchesM1=!m1||JSON.parse(el.dataset.m1).includes(m1);
const matchesEvent=(!task&&!stage)||JSON.parse(el.dataset.events).some(event=>(!task||event.task===task)&&(!stage||event.stage===stage));
el.hidden=!(matchesM1&&matchesEvent);
});
document.getElementById('visible-item-count').textContent=String(Array.from(document.querySelectorAll('.progress-item')).filter(el=>!el.hidden).length);
}document.querySelectorAll('select').forEach(el=>el.addEventListener('change',applyFilters));applyFilters();'''

    page = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TCO 任务贡献与阶段进展</title><style>body{max-width:1100px;margin:2rem auto;padding:0 1rem;font-family:system-ui;line-height:1.6;color:#17324a;background:#f5f8fb}article{border:1px solid #afc2d1;padding:1rem;margin:1rem 0;background:white;border-radius:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#e7eef5;padding:1rem}a{overflow-wrap:anywhere}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;vertical-align:top;border:1px solid #afc2d1;padding:.5rem;overflow-wrap:anywhere}th{background:#dce9f2}details{margin:1rem 0}nav{position:sticky;top:0;background:#dce9f2;padding:1rem;display:flex;gap:1rem;flex-wrap:wrap}[hidden]{display:none!important}</style></head><body><h1>TCO 任务贡献与阶段进展</h1><nav>' + ''.join(filters) + '</nav>' + ''.join(sections) + '<script>' + javascript + '</script></body></html>'
    return '\n'.join(markdown).encode('utf-8'), page.encode('utf-8')


def main(argv=None):
    global SCHEMA
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'render'))
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--progress', required=True)
    parser.add_argument('--base-progress')
    parser.add_argument('--output-dir')
    args = parser.parse_args(argv)
    temporary = None
    try:
        SCHEMA = load(Path(__file__).resolve().parent.parent / 'references' / 'tco-progress.schema.json')
        workspace = Path(args.workspace).resolve()
        need(workspace.is_dir(), 'workspace 不存在')
        progress = local_path(workspace, args.progress)
        explicit_base = local_path(workspace, args.base_progress) if args.base_progress else None
        result = validate(workspace, progress, explicit_base)
        audit = {'schema': 'tco_progress_audit.v1', 'verdict': 'pass', 'authority_note': NOTICE,
                 'product_id': result['doc']['product_id'], 'snapshot_id': result['doc']['snapshot_id'],
                 'progress': {'path': args.progress, 'sha256': result['progress_sha256']},
                 'snapshot_ids': result['snapshot_ids'], 'before': result['doc']['before'], 'after': result['doc']['after'],
                 'item_count': len(result['items']), 'new_event_count': len(result['new_events']),
                 'task_ids': result['doc']['tasks'], 'new_events': result['new_events']}
        audit['task_contributions'] = {task: {'event_count': sum(e['task_id'] == task for e in result['new_events']), 'item_ids': sorted({e['item_id'] for e in result['new_events'] if e['task_id'] == task})} for task in result['doc']['tasks']}
        audit['summaries'] = {side: {'m1_count': len(result[side]['m1_ids']), 'profile_count': len(result[side]['profiles']), 'coverage': 'unknown' if result[side]['report'] is None else result[side]['report']['coverage_mode'], 'profiles': None if result[side]['report'] is None else result[side]['report']['target_profiles']} for side in ('before', 'after')}
        if args.command == 'render':
            need(args.output_dir, 'render 需要 --output-dir')
            output_arg = Path(args.output_dir)
            output = (output_arg if output_arg.is_absolute() else workspace / output_arg).absolute()
            need(not output.exists() and not output.is_symlink(), '拒绝覆盖既有输出目录')
            need(output.parent.is_dir(), '输出父目录不存在')
            markdown, page = rendering(result, workspace, output)
            audit['outputs'] = {'progress.md': digest(markdown), 'progress.html': digest(page)}
            temporary = Path(tempfile.mkdtemp(prefix='.tco-progress-', dir=str(output.parent)))
            (temporary / 'progress.md').write_bytes(markdown)
            (temporary / 'progress.html').write_bytes(page)
            (temporary / 'audit.json').write_bytes(json.dumps(audit, ensure_ascii=False, indent=2).encode('utf-8') + b'\n')
            # mkdir gives no-overwrite publication, including an existing empty directory.
            output.mkdir()
            try:
                for child in temporary.iterdir():
                    os.replace(str(child), str(output / child.name))
            except BaseException:
                shutil.rmtree(output)
                raise
        else:
            need(args.output_dir is None, 'validate 不接受 --output-dir')
        print(json.dumps(audit, ensure_ascii=False, sort_keys=True))
        return 0
    except (Invalid, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(json.dumps({'verdict': 'fail', 'findings': [str(exc)]}, ensure_ascii=False))
        print('进展校验失败：' + str(exc), file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
