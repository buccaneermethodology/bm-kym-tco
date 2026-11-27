#!/usr/bin/env python3
"""只读候选 E/C/Q 引擎。Python 3.9+；仅可选 ECQ 路径依赖 jsonschema。"""
import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
import types

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parent / 'references' / 'ecq-assessment.schema.json'


class ECQError(ValueError):
    def __init__(self, code, path, message):
        self.code, self.path, self.message = code, str(path), str(message)
        super().__init__(code + ' ' + self.path + ': ' + self.message)

    def as_dict(self):
        return {'code': self.code, 'path': self.path, 'message': self.message}


def require(ok, code, path, message):
    if not ok:
        raise ECQError(code, path, message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(data, path='$'):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, 'ECQ-JSON', path, '重复 JSON key: ' + key)
            result[key] = value
        return result
    def finite(value):
        raise ECQError('ECQ-JSON', path, '非有限数值: ' + value)
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=finite)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ECQError):
            raise
        raise ECQError('ECQ-JSON', path, str(exc)) from exc
    def walk(v):
        if isinstance(v, float):
            require(math.isfinite(v), 'ECQ-JSON', path, '非有限数值')
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(value)
    return value


def pointer(value, path):
    require(isinstance(path, str) and (path == '' or path.startswith('/')), 'ECQ-POINTER', path, '必须为 RFC6901 pointer')
    try:
        for token in path.split('/')[1:]:
            require(not re.search(r'~(?![01])', token), 'ECQ-POINTER', path, '非法转义')
            token = token.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                require(re.fullmatch(r'0|[1-9][0-9]*', token) is not None, 'ECQ-POINTER', path, '非法数组索引')
                value = value[int(token)]
            else:
                require(isinstance(value, dict), 'ECQ-POINTER', path, '不能继续定位标量')
                value = value[token]
        return value
    except (KeyError, IndexError, TypeError) as exc:
        raise ECQError('ECQ-POINTER', path, '定位不存在') from exc


def timepoint(value, path='$', code='ECQ-TIME'):
    from datetime import datetime
    require(isinstance(value, str) and 'T' in value, code, path, '时间必须带时区')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ECQError(code, path, '非法时间') from exc
    require(result.tzinfo is not None and result.utcoffset() is not None, code, path, '时间缺少时区')
    return result


def fraction(value):
    return Fraction(str(value))


def unique(rows, key, path):
    result = {}
    for row in rows:
        require(row[key] not in result, 'ECQ-ID', path, '重复 ' + key + ': ' + row[key])
        result[row[key]] = row
    return result


def display(low, high, all_unknown=False, core='available'):
    def percent(value):
        number = Decimal(value.numerator) / Decimal(value.denominator) if isinstance(value, Fraction) else Decimal(str(value))
        return format((number * 100).quantize(Decimal('.1'), rounding=ROUND_HALF_UP), 'f').rstrip('0').rstrip('.') + '%'
    if low is None or high is None or all_unknown:
        mode, text = 'unknown', '未知'
    elif fraction(high) - fraction(low) > Fraction(1, 4):
        mode, text = 'insufficient_data', '资料不足，暂不能给出可靠整体估算'
    elif low == high:
        mode, text = 'point', percent(low)
    else:
        mode, text = 'range', percent(low) + '—' + percent(high)
    detail = text if low is None else (percent(low) if low == high else percent(low) + '—' + percent(high))
    if core == 'unavailable' and low is not None and not all_unknown:
        mode, text = 'core_unavailable', '核心行为未具备'
    return {'mode': mode, 'text': text, 'detail_text': detail, 'core_text': '核心行为待确认' if core == 'unknown' else ('核心行为未具备' if core == 'unavailable' else '')}


class Context:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        require(self.workspace.is_dir(), 'ECQ-PATH', '.', 'workspace不存在')
        self.bindings = {}
        self.progress_cache = {}
        self.assessment_cache = {}
        self.warnings = []
        self.progress = types.ModuleType('_ecq_progress')
        helper = HERE / 'progress.py'
        # compile+exec avoids __pycache__ mutations in installed Skill identity.
        exec(compile(helper.read_bytes(), str(helper), 'exec'), self.progress.__dict__)
        self.progress.SCHEMA = strict_json((HERE.parent / 'references' / 'tco-progress.schema.json').read_bytes())
        self.schema = strict_json(SCHEMA_PATH.read_bytes())
        self.validator = Draft202012Validator(self.schema)
        for path, role in [(Path(__file__).resolve(), 'engine'), (helper, 'progress-helper'),
                           (SCHEMA_PATH, 'schema'), (HERE.parent / 'references' / 'tco-progress.schema.json', 'progress-schema')]:
            self.bindings[(role, path.name)] = {'base': 'skill', 'path': str(path.relative_to(HERE.parent)), 'sha256': digest(path.read_bytes()), 'role': role}

    def path(self, value):
        require(isinstance(value, str) and value.strip() and not Path(value).is_absolute(), 'ECQ-PATH', value, '必须为workspace相对路径')
        require('\\' not in value and not re.match(r'^[A-Za-z]:', value), 'ECQ-PATH', value, '非便携或非法路径')
        p = (self.workspace / value).resolve()
        try:
            p.relative_to(self.workspace)
        except ValueError as exc:
            raise ECQError('ECQ-PATH', value, '路径或symlink越过workspace') from exc
        require(p.is_file(), 'ECQ-PATH', value, '文件不存在')
        return p

    def read(self, value, role='source', expected=None, json_value=True):
        p = self.path(value)
        data = p.read_bytes()
        actual = digest(data)
        require(expected is None or actual == expected, 'ECQ-DIGEST', value, '字节摘要漂移')
        self.bindings[(role, value)] = {'path': value, 'sha256': actual, 'role': role}
        return strict_json(data, value) if json_value else data

    def binding(self, binding, role='source', json_value=True):
        return self.read(binding['path'], role, binding['sha256'], json_value)

    def shape(self, value, definition, code):
        errors = sorted(Draft202012Validator({'$ref': '#/$defs/' + definition, '$defs': self.schema['$defs']}).iter_errors(value), key=lambda e: str(e.path))
        require(not errors, code, definition, errors[0].message if errors else '')

    def archive(self, binding, chain=(), allow_missing=False):
        """复用 progress helper；仅历史成员门禁适配，其他原门禁逐项保留。"""
        key = (binding['path'], binding['sha256'], allow_missing)
        require(key not in chain, 'ECQ-HISTORY-CYCLE', binding['path'], '祖先循环')
        if key in self.progress_cache:
            return self.progress_cache[key]
        p = self.progress
        d = self.binding(binding, 'progress')
        def need(ok, code, message):
            require(ok, code, binding['path'], message)
        try:
            p.shape(d, p.SCHEMA)
            cutoff = timepoint(d['cutoff'], binding['path'], 'ECQ-HISTORY-TIME')
            for side in ['before', 'after']:
                model = self.binding(d[side]['model'], 'progress-side')
                require(model.get('product_id') == d['product_id'], 'ECQ-HISTORY-IDENTITY', binding['path'], '历史产品身份不一致')
            missing_base = False
            try:
                base = self.archive(d['base_progress'], chain + (key,), allow_missing) if d['base_progress'] else None
            except ECQError as exc:
                if allow_missing and exc.code == 'ECQ-PATH' and exc.message == '文件不存在' and d['base_progress'] and exc.path == d['base_progress']['path']:
                    base, missing_base = None, True
                    self.warnings.append({'code': 'ECQ-HISTORY-PARTIAL', 'path': exc.path, 'message': '祖先缺失；仅核对已接入链，事件首次引入身份未完整证明，不宣称全历史完整'})
                else:
                    raise
            # Helpers route through this Context, so nested evidence remains in audit.
            old_ref, old_load = p.reference, p.load
            def checked_ref(workspace, ref):
                self.binding(ref, 'progress-evidence', json_value=False)
                return self.path(ref['path'])
            p.reference = checked_ref
            p.load = lambda path: self.read(str(Path(path).relative_to(self.workspace)), 'progress-side')
            try:
                before = p.side_data(self.workspace, d['before'], d['product_id'])
                after = p.side_data(self.workspace, d['after'], d['product_id'])
                def evidence(refs, required=True):
                    p.evidence(self.workspace, refs, required)
                items, requirements = p.by_id(d['items'], 'items'), p.by_id(d['requirements'], 'requirements')
                included, excluded = set(d['included_stages']), set(d['excluded_stages'])
                need(len(excluded) == len(d['excluded_stages']) and not included & excluded, 'ECQ-HISTORY-STAGE', '阶段重复或冲突')
                valid_m = before['m1_ids'] | after['m1_ids']
                historical_m = valid_m | (base['historical_m'] if base else set())
                retired_adapter = False
                if base:
                    old = base['doc']
                    need(d['product_id'] == old['product_id'], 'ECQ-HISTORY-IDENTITY', '产品身份不匹配')
                    need(d['snapshot_id'] not in base['snapshots'], 'ECQ-HISTORY-CYCLE', 'snapshot重复')
                    need(cutoff > timepoint(old['cutoff']), 'ECQ-HISTORY-TIME', 'cutoff未递增')
                    need(all(d['before'][k] == old['after'][k] for k in ('model', 'coverage')), 'ECQ-HISTORY-BINDING', '前后绑定断链')
                    need(set(old['included_stages']) <= included, 'ECQ-HISTORY-STAGE', '累计阶段丢失')
                    need(set(base['items']) <= set(items) and set(base['requirements']) <= set(requirements), 'ECQ-HISTORY-PREFIX', '历史ledger删除')
                changes = {}
                for change in d['changes']:
                    k = (change['kind'], change['id'])
                    need(k not in changes, 'ECQ-HISTORY-CHANGE', '重复change')
                    changes[k] = change
                    evidence(change['evidence_refs'])
                actual = {(kind, k) for kind, col in [('node', 'nodes'), ('profile', 'profiles')]
                          for k in set(before[col]) | set(after[col]) if before[col].get(k) != after[col].get(k)}
                need(set(changes) == actual, 'ECQ-HISTORY-CHANGE', 'model/Profile变化未精确处置')
                for rid, req in requirements.items():
                    previous = base['requirements'].get(rid) if base else None
                    need(set(req['m1_ids']) <= historical_m, 'ECQ-HISTORY-MEMBERSHIP', '要求M从未存在')
                    if not set(req['m1_ids']) <= valid_m:
                        retired_adapter = True
                        need(previous is not None and req == previous, 'ECQ-HISTORY-MEMBERSHIP', '移除M要求必须保持原记录')
                    evidence(req['evidence_refs'], req['disposition'] != 'retained')
                    need(req['disposition'] == 'retained' or req['reason'].strip(), 'ECQ-HISTORY-CHANGE', '处置无理由')
                    if previous:
                        changed = any(req[k] != previous[k] for k in ('text', 'm1_ids'))
                        need(not changed or req['disposition'] == 'revised', 'ECQ-HISTORY-CHANGE', '要求变化未revised')
                        need(req['disposition'] == previous['disposition'] or req['disposition'] != 'retained', 'ECQ-HISTORY-CHANGE', '不可静默恢复retained')
                    elif not base and not missing_base:
                        need(req['disposition'] == 'retained', 'ECQ-HISTORY-CHANGE', '初始要求非retained')
                new_events, identities = [], dict(base['identities']) if base else {}
                for iid, item in items.items():
                    previous = base['items'].get(iid) if base else None
                    need(set(item['m1_ids']) <= historical_m, 'ECQ-HISTORY-MEMBERSHIP', '推进项M从未存在')
                    need(set(item['requirement_ids']) <= set(requirements), 'ECQ-HISTORY-MEMBERSHIP', '原子要求不存在')
                    linked = {m for rid in item['requirement_ids'] for m in requirements[rid]['m1_ids']}
                    need(set(item['m1_ids']) <= linked, 'ECQ-HISTORY-MEMBERSHIP', '共享item要求关联不完整')
                    prefix = len(previous['events']) if previous else 0
                    if previous:
                        need(all(item[k] == previous[k] for k in ('m1_ids', 'requirement_ids', 'title', 'acceptance')), 'ECQ-HISTORY-PREFIX', 'item稳定语义被改写')
                        need(item['events'][:prefix] == previous['events'], 'ECQ-HISTORY-PREFIX', 'event前缀被改写')
                    if not set(item['m1_ids']) <= valid_m:
                        need(previous is not None and len(item['events']) == prefix, 'ECQ-HISTORY-REMOVED-NEW', '移除M不可产生新贡献')
                    state, last = 'unknown', None
                    for index, event in enumerate(item['events']):
                        observed = timepoint(event['observed_at'], binding['path'], 'ECQ-HISTORY-TIME')
                        need(event['before'] == state, 'ECQ-HISTORY-STATE', 'before/after断链')
                        need((last is None or observed > last) and observed <= cutoff, 'ECQ-HISTORY-TIME', '事件时点不合法')
                        need(event['stage'] in included and event['stage'] not in excluded, 'ECQ-HISTORY-STAGE', '事件阶段未纳入')
                        evidence(event['evidence_refs'])
                        if event['outcome'] in ('no_change', 'evidence_only'):
                            need(event['before'] == event['after'], 'ECQ-HISTORY-STATE', '无变化事件改变状态')
                        if event['outcome'] == 'forward':
                            need(event['after'] != event['before'] and event['after'] not in ('unknown', 'blocked'), 'ECQ-HISTORY-STATE', 'forward无明确推进')
                        if event['outcome'] == 'regression':
                            need(event['after'] != event['before'], 'ECQ-HISTORY-STATE', 'regression无状态变化')
                        if index >= prefix:
                            need(missing_base or event['task_id'] in d['tasks'], 'ECQ-HISTORY-TASK', '新增事件任务未归属')
                            need(not base or observed > timepoint(base['doc']['cutoff']), 'ECQ-HISTORY-TIME', '新增事件不晚于base')
                            if not missing_base or event['task_id'] in d['tasks']:
                                new_events.append(event)
                            identities[(iid, index)] = ('partial:' if missing_base else '') + d['snapshot_id'] + ':' + iid + ':' + str(index)
                        state, last = event['after'], observed
                need(set(d['tasks']) == {e['task_id'] for e in new_events}, 'ECQ-HISTORY-TASK', 'task与新增事件不相等')
            finally:
                p.reference, p.load = old_ref, old_load
            result = {'doc': d, 'items': items, 'requirements': requirements, 'historical_m': historical_m,
                      'identities': identities, 'snapshots': (base['snapshots'] if base else []) + [d['snapshot_id']], 'history_complete': not missing_base and (base['history_complete'] if base else True)}
            if retired_adapter and result['history_complete']:
                try:
                    p.validate(self.workspace, self.path(binding['path']))
                except p.Invalid as exc:
                    self.warnings.append({'code': 'ECQ-ORIGINAL-PROGRESS-FAILED', 'path': binding['path'], 'message': '原progress工具failed（失败）保留：' + str(exc) + '；ECQ受限适配不改变该结果'})
            self.progress_cache[key] = result
            return result
        except p.Invalid as exc:
            raise ECQError('ECQ-HISTORY-VALIDATION', binding['path'], str(exc)) from exc

    def derivation(self, source, output):
        d = source['derivation']
        if not d:
            return True
        timepoint(d['extracted_at'], source['id'])
        rule = self.binding(d['extraction_rule_ref'], 'extraction-rule')
        self.shape(rule, 'extraction_rule', 'ECQ-EXTRACTION-RULE')
        require(d['extraction_rule_id'] == rule['id'] and d['extraction_rule_version'] == rule['version'], 'ECQ-EXTRACTION-RULE', source['id'], '规则身份不匹配')
        require([o['binding'] for o in d['origins']] == rule['origins'], 'ECQ-EXTRACTION-RULE', source['id'], '原文binding不匹配')
        targets = unique(rule['fields'], 'target_pointer', source['id'])
        origin_bytes = [self.binding(b, 'origin', False) for b in rule['origins']]
        def text_value(text, kind):
            text = text.strip()
            require(bool(text), 'ECQ-EXTRACTION-VALUE', source['id'], '空字段不能造null')
            if kind == 'string':
                return text
            if kind == 'string_or_null':
                return None if text == 'null' else text
            require(text in (('true', 'false') if kind == 'boolean' else ('null',)), 'ECQ-EXTRACTION-VALUE', source['id'], '字面量不匹配')
            return text == 'true' if kind == 'boolean' else None
        def cells(line):
            require(line.startswith('|') and line.endswith('|') and '\\' not in line, 'ECQ-EXTRACTION-VALUE', source['id'], '不支持的表格')
            return [x.strip() for x in line[1:-1].split('|')]
        for f in targets.values():
            try:
                lines = origin_bytes[f['origin_index']].decode('utf-8').splitlines()
                line = lines[f['line'] - 1]
                syntax = f.get('syntax', 'line_json_literal')
                if syntax == 'line_json_literal':
                    require(line.startswith(f['prefix']), 'ECQ-EXTRACTION-VALUE', f['target_pointer'], 'prefix不匹配')
                    value = strict_json(line[len(f['prefix']):], f['target_pointer'])
                elif syntax == 'key_value_line':
                    require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=', f['prefix']) and line.startswith(f['prefix']), 'ECQ-EXTRACTION-VALUE', f['target_pointer'], 'key=value定位错误')
                    text = line[len(f['prefix']):]
                    require(not any(c in text for c in ['=', '#', '\\', '"']), 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '不支持的字段语法')
                    value = text_value(text, f['value_type'])
                else:
                    require(not any(x.strip().startswith(('```', '~~~')) for x in lines[:f['line']]), 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '不支持代码表格')
                    require(f['header_line'] + 1 == f['separator_line'] < f['line'], 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '表格行定位错误')
                    headers = cells(lines[f['header_line'] - 1])
                    require(headers == f['headers'] and len(set(headers)) == len(headers), 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '表头不匹配')
                    sep = cells(lines[f['separator_line'] - 1])
                    require(len(sep) == len(headers) and all(re.fullmatch(r':?-{3,}:?', x) for x in sep), 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '非法separator')
                    row = cells(line)
                    require(len(row) == len(headers) and row[headers.index(f['key_column'])] == f['key_value'], 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '表格key不匹配')
                    matches = 0
                    for following in lines[f['separator_line']:]:
                        if not following.startswith('|'):
                            break
                        cr = cells(following)
                        require(len(cr) == len(headers), 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '列数不匹配')
                        matches += cr[headers.index(f['key_column'])] == f['key_value']
                    require(matches == 1, 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '重复key行')
                    value = text_value(row[headers.index(f['column'])], f['value_type'])
                actual = pointer(output, f['target_pointer'])
                require(type(value) is type(actual) and value == actual, 'ECQ-EXTRACTION-VALUE', f['target_pointer'], '派生值与原文不一致')
            except (IndexError, KeyError, ValueError, UnicodeError) as exc:
                if isinstance(exc, ECQError):
                    raise
                raise ECQError('ECQ-EXTRACTION-VALUE', f['target_pointer'], str(exc)) from exc
        for index, origin in enumerate(d['origins']):
            data = origin_bytes[index]
            rows = [f for f in targets.values() if f['origin_index'] == index]
            kind, selector = origin['selector_kind'], origin['selector']
            if kind == 'line_range':
                m = re.fullmatch(r'L([1-9][0-9]*)-L([1-9][0-9]*)', selector)
                require(m is not None, 'ECQ-EXTRACTION-RULE', source['id'], '非法line selector')
                a, b = map(int, m.groups())
                require(1 <= a <= b <= len(data.decode('utf-8').splitlines()) and all(a <= f['line'] <= b and (f.get('syntax') != 'markdown_table' or a <= f['header_line'] <= f['separator_line'] <= b) for f in rows), 'ECQ-EXTRACTION-RULE', source['id'], 'selector未覆盖消费行')
            elif kind == 'utf8_byte_range':
                m = re.fullmatch(r'B([0-9]+)-B([0-9]+)', selector)
                require(m is not None, 'ECQ-EXTRACTION-RULE', source['id'], '非法byte selector')
                a, b = map(int, m.groups())
                require(0 <= a <= b <= len(data), 'ECQ-EXTRACTION-RULE', source['id'], 'byte越界')
                try:
                    data[:a].decode(); data[a:b].decode(); data[b:].decode()
                except UnicodeError as exc:
                    raise ECQError('ECQ-EXTRACTION-RULE', source['id'], '切断UTF8') from exc
                offsets = [0]
                for line in data.splitlines(keepends=True):
                    offsets.append(offsets[-1] + len(line))
                require(all(a <= offsets[f['line'] - 1] and offsets[f['line']] <= b for f in rows), 'ECQ-EXTRACTION-RULE', source['id'], 'byte selector未覆盖行')
            else:
                pointer(strict_json(data), selector)
                require(not rows, 'ECQ-EXTRACTION-RULE', source['id'], '行规则不能使用JSON selector')
        if not d['verification_ref']:
            return False
        v = self.binding(d['verification_ref'], 'extraction-verification')
        self.shape(v, 'extraction_verification', 'ECQ-EXTRACTION-VERIFICATION')
        unique(v['field_checks'], 'target_pointer', source['id'])
        require(timepoint(v['reviewed_at']) >= timepoint(d['extracted_at']), 'ECQ-TIME', source['id'], '核对早于抽取')
        if v['rule'] != d['extraction_rule_ref'] or v['output'] != source['binding'] or v['origins'] != rule['origins']:
            return False
        expected = {(f['target_pointer'], f['origin_index'], f['line'], digest(canonical(pointer(output, f['target_pointer'])))) for f in targets.values()}
        actual = {(f['target_pointer'], f['origin_index'], f['line'], f['value_sha256']) for f in v['field_checks']}
        require(actual == expected, 'ECQ-EXTRACTION-VERIFICATION', source['id'], '核对字段集合/值不匹配')
        return True

    def evaluate(self, assessment, chain=()):
        require(assessment not in chain, 'ECQ-SCOPE-BASE', assessment, 'base assessment循环')
        if assessment in self.assessment_cache:
            return self.assessment_cache[assessment]
        doc = self.read(assessment, 'assessment')
        cutoff = timepoint(doc.get('cutoff'), '/cutoff')
        for b in doc.get('business_requirements', []):
            require(bool(b.get('required_condition_refs')), 'ECQ-CONDITIONS', b.get('id'), '原必需条件不能省略')
            require(isinstance(b.get('weight'), (int, float)) and not isinstance(b.get('weight'), bool) and b['weight'] > 0, 'ECQ-WEIGHT', b.get('id'), '权重必须为正数')
        for e in doc.get('estimates', []):
            lo, hi = e.get('low'), e.get('high')
            require((lo is None) == (hi is None), 'ECQ-UNKNOWN-NUMERIC', e.get('mapping_id'), '未知必须双null')
            if lo is not None:
                require(isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and not isinstance(lo, bool) and not isinstance(hi, bool) and 0 <= lo <= hi <= 1, 'ECQ-RANGE', e.get('mapping_id'), '份额区间非法')
        for source in doc.get('sources', []):
            require((source.get('availability') == 'connected') == (source.get('binding') is not None), 'ECQ-SOURCE', source.get('id'), '连接状态与binding不一致')
            if source.get('binding'):
                self.path(source['binding']['path'])
        errors = sorted(self.validator.iter_errors(doc), key=lambda e: str(e.path))
        require(not errors, 'ECQ-SCHEMA', '/' + '/'.join(map(str, errors[0].path)) if errors else assessment, errors[0].message if errors else '')
        sources = unique(doc['sources'], 'id', '/sources')
        raw = {sid: self.binding(s['binding']) for sid, s in sources.items() if s['binding']}
        def ref(r):
            require(r['source_id'] in raw, 'ECQ-SOURCE', r['source_id'], '来源未接入或不存在')
            return pointer(raw[r['source_id']], r['pointer'])
        def walk(v):
            if isinstance(v, dict):
                if set(v) == {'source_id', 'pointer'}:
                    ref(v)
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        walk(doc)
        archives = {sid: self.archive(s['binding'], allow_missing=doc['scope']['session_history_completeness'] == 'partial') for sid, s in sources.items() if s['kind'] == 'progress' and s['binding']}
        trusted = {sid: self.derivation(s, raw[sid]) for sid, s in sources.items() if s['binding']}
        for sid, source in sources.items():
            if source['derivation']:
                rule = self.binding(source['derivation']['extraction_rule_ref'], 'extraction-rule')
                targets = {f['target_pointer'] for f in rule['fields']}
                fields = []
                for link in doc['session_links']:
                    if link['record_ref']['source_id'] == sid:
                        fields.extend(link['record_ref']['pointer'] + p for p in link['fields'].values() if p is not None)
                for c in doc['quality_checks']:
                    if c['result_ref'] and c['result_ref']['source_id'] == sid:
                        fields.extend(c['result_ref']['pointer'] + p for p in c['fields'].values())
                require(set(fields) <= targets, 'ECQ-EXTRACTION-RULE', sid, '规则未覆盖所有消费字段')
        ledger = unique(doc['business_requirements'], 'id', '/business_requirements')
        mappings = unique(doc['mappings'], 'id', '/mappings')
        estimates = unique(doc['estimates'], 'mapping_id', '/estimates')
        sessions = unique(doc['session_links'], 'task_id', '/session_links')
        session_bindings = {}
        for link in doc['session_links']:
            identity = (link['record_ref'], link['fields'])
            require(link['session_id'] not in session_bindings or session_bindings[link['session_id']] == identity, 'ECQ-SESSION-MAPPING', link['session_id'], '同一Session绑定冲突原记录')
            session_bindings[link['session_id']] = identity
        unique(doc['contributions'], 'id', '/contributions')
        caps = unique(doc['capability_catalog'], 'm1_id', '/capability_catalog')
        steps = unique(doc['next_steps'], 'id', '/next_steps')
        scope = doc['scope']
        current_model = raw.get(scope['current_model_source_id'])
        require(current_model is not None, 'ECQ-SOURCE', scope['current_model_source_id'], '无法定位当前M；仅KYM不能生成M投影')
        require(current_model.get('product_id') == doc['product_id'], 'ECQ-IDENTITY', '/scope', 'model产品不一致')
        require(current_model.get('candidate_status') == 'candidate_not_approved', 'ECQ-IDENTITY', '/scope', '仅接受候选model')
        require(set(scope['tp_ids']) <= {t['id'] for t in current_model['target_profiles']}, 'ECQ-TP', '/scope/tp_ids', 'TP不存在')
        current_m = {n['id'] for n in current_model['nodes'] if n['node_kind'] == 'm1'}
        require(current_m <= set(caps), 'ECQ-MAPPING', '/capability_catalog', '当前M缺少来源定义')
        for sid, s in sources.items():
            if s['kind'] == 'model' and sid in raw:
                require(raw[sid].get('product_id') == doc['product_id'] and raw[sid].get('candidate_status') == 'candidate_not_approved', 'ECQ-IDENTITY', sid, 'model身份/候选状态不一致')
            if s['kind'] == 'coverage' and sid in raw:
                cov = raw[sid]
                matches = [v for mid, v in raw.items() if sources[mid]['kind'] == 'model' and v.get('product_id') == doc['product_id'] and v.get('candidate_status') == 'candidate_not_approved' and cov.get('source_tco_model_digest') == 'sha256:' + digest(canonical(v))]
                require(bool(matches) and cov.get('product_id') == doc['product_id'] and cov.get('candidate_status') == 'candidate_not_approved', 'ECQ-IDENTITY', sid, 'Coverage/model绑定不一致')
        coverage_id = scope['current_coverage_source_id']
        if coverage_id in raw:
            require(raw[coverage_id].get('source_tco_model_digest') == 'sha256:' + digest(canonical(current_model)), 'ECQ-IDENTITY', coverage_id, '当前Coverage未绑定当前model')
        report_id = scope['current_report_source_id']
        tp_target = 'unknown'
        if report_id is not None and report_id in raw:
            require(coverage_id in raw, 'ECQ-IDENTITY', report_id, 'report缺当前Coverage')
            side = {'model': sources[scope['current_model_source_id']]['binding'], 'coverage': sources[coverage_id]['binding'], 'report': sources[report_id]['binding']}
            try:
                self.progress.side_data(self.workspace, side, doc['product_id'])
            except self.progress.Invalid as exc:
                raise ECQError('ECQ-IDENTITY', report_id, str(exc)) from exc
            tp_target = {pid: next(p for p in raw[report_id]['target_profiles'] if p['id'] == pid) for pid in scope['tp_ids']}
        require(set(scope['requirement_ids']) == set(ref(scope['definition_ref'])['requirement_ids']) and set(scope['requirement_ids']) <= set(ledger), 'ECQ-SCOPE', '/scope/requirement_ids', '分母与原清单不相等')
        for mid, c in caps.items():
            definition = ref(c['definition_ref'])
            require(definition.get('id') == mid and definition.get('node_kind') == 'm1', 'ECQ-MAPPING', mid, 'M定义不匹配')
        for bid, b in ledger.items():
            definition = ref(b['definition_ref'])
            require(definition.get('id') == bid, 'ECQ-MAPPING', bid, '原要求ID不一致')
            require(set(b['mapping_ids']) == {m['id'] for m in mappings.values() if m['business_requirement_id'] == bid} and len(b['mapping_ids']) == len(set(b['mapping_ids'])), 'ECQ-MAPPING', bid, 'ledger职责集合不一致')
            conditions = [ref(r)['id'] for r in b['required_condition_refs']]
            require(len(conditions) == len(set(conditions)) and set(conditions) == {c['id'] for c in definition['conditions']}, 'ECQ-CONDITIONS', bid, '原必需条件被省略/重复')
            require(scope['weight_policy'] != 'equal_original_requirements' or b['weight'] == 1, 'ECQ-WEIGHT', bid, '等权政策要求权重1')
            if scope['weight_policy'] == 'explicit_business_weights':
                source_weight = definition.get('weight', ref(scope['definition_ref']).get('weights', {}).get(bid))
                require(source_weight == b['weight'], 'ECQ-WEIGHT', bid, '业务权重缺原来源明确定义')
        for mid, m in mappings.items():
            bid = m['business_requirement_id']
            require(bid in ledger and mid in ledger[bid]['mapping_ids'] and m['m1_id'] in caps, 'ECQ-MAPPING', mid, '职责归属不一致')
            if m['resolution'] != 'exact':
                require(not m['tp_requirement_refs'] and not m['atomic_requirement_refs'] and not m['progress_item_refs'] and m['relation'] == 'unknown', 'ECQ-MAPPING', mid, '未知映射不能保留精确关联')
                continue
            require(m['tp_requirement_refs'] and m['atomic_requirement_refs'], 'ECQ-MAPPING', mid, '精确映射缺TP/原子要求')
            require(set(m['coverage_requirement_ids']) == {ref(a)['id'] for a in m['atomic_requirement_refs']}, 'ECQ-MAPPING', mid, '原子要求ID不一致')
            for r in m['tp_requirement_refs']:
                row = ref(r)
                require(row.get('source_requirement_ref') == bid and m['m1_id'] in row['node_ids'], 'ECQ-MAPPING', mid, 'TP原要求/节点不一致')
                parents = [tp for tp in raw[r['source_id']]['target_profiles'] if any(x is row for x in tp['requirements'])]
                require(len(parents) == 1 and parents[0]['id'] in scope['tp_ids'], 'ECQ-TP', mid, '职责不属于选中TP')
            for r in m['atomic_requirement_refs']:
                row = ref(r)
                require(sources[r['source_id']]['kind'] == 'model' and isinstance(raw[r['source_id']].get('nodes'), list), 'ECQ-MAPPING', mid, 'atomic_requirement_refs 来源 ' + r['source_id'] + ' 必须是实际model的 /nodes/.../coverage_requirements/...；progress.requirements 是另一类记录')
                nodes = [n for n in raw[r['source_id']]['nodes'] if any(a is row for a in n.get('coverage_requirements', []))]
                require(len(nodes) == 1 and nodes[0]['id'] == m['m1_id'], 'ECQ-MAPPING', mid, '原子要求属于其他M')
            for r in m['progress_item_refs']:
                item = ref(r)
                reqs = {q['id']: q for q in raw[r['source_id']]['requirements']}
                require(m['m1_id'] in item['m1_ids'] and set(m['coverage_requirement_ids']) <= set(item['requirement_ids']) and all(m['m1_id'] in reqs[rid]['m1_ids'] for rid in m['coverage_requirement_ids']), 'ECQ-MAPPING', mid, 'progress item/原子职责不匹配')
        active = {m['id'] for m in mappings.values() if m['business_requirement_id'] in scope['requirement_ids']}
        require(set(estimates) == active, 'ECQ-SCOPE', '/estimates', '估计未完整覆盖当前职责')
        intervals = {}
        for mid, e in estimates.items():
            require(timepoint(e['assessed_at'], mid) <= cutoff, 'ECQ-TIME', mid, '估计晚于cutoff')
            require(e['applies_to_version'] == scope['product_version'], 'ECQ-ESTIMATE-VERSION', mid, '实现估计适用版本不匹配')
            if e['low'] is None:
                require(e['method'] == 'unknown' and e['basis_kind'] == 'unknown' and e['basis_ref'] is None, 'ECQ-UNKNOWN-NUMERIC', mid, '未知估计必须保留未知依据')
                intervals[mid] = (Fraction(0), Fraction(1))
                continue
            require(mappings[mid]['resolution'] == 'exact' and e['basis_ref'] is not None, 'ECQ-BASIS', mid, '已知估计必须精确映射及真实依据')
            basis = ref(e['basis_ref'])
            bid = mappings[mid]['business_requirement_id']
            require(basis.get('id') == mid or (len(ledger[bid]['mapping_ids']) == 1 and basis.get('id') == bid), 'ECQ-BASIS', mid, '份额依据属于其他职责')
            require(basis.get('core_behavior') == e['core_behavior'], 'ECQ-CORE-EVIDENCE', mid, '核心可用性与来源矛盾')
            require(basis.get('low') == e['low'] and basis.get('high') == e['high'], 'ECQ-BASIS', mid, '份额与依据不相符')
            if e['method'] == 'acceptance_share':
                parts = unique(basis.get('parts', []), 'id', mid)
                require(parts and sum(fraction(p['share']) for p in parts.values()) == 1 and all(p['share'] > 0 and 0 <= p['implemented_fraction'] <= 1 for p in parts.values()), 'ECQ-BASIS', mid, '验收份额不完整')
                total = sum(fraction(p['share']) * fraction(p['implemented_fraction']) for p in parts.values())
                require(total == fraction(e['low']) == fraction(e['high']), 'ECQ-BASIS', mid, '验收份额加权不相等')
                core = [p for p in parts.values() if p['core']]
                if core and e['core_behavior'] == 'known_available':
                    require(any(p['implemented_fraction'] > 0 for p in core), 'ECQ-CORE-EVIDENCE', mid, '核心份额为零却声明可用')
                if core and e['core_behavior'] == 'known_unavailable':
                    require(any(p['implemented_fraction'] < 1 for p in core), 'ECQ-CORE-EVIDENCE', mid, '核心份额均具备却声明不可用')
            intervals[mid] = (fraction(e['low']), fraction(e['high']))
        parent_intervals, paths = {}, {}
        for bid in scope['requirement_ids']:
            b = ledger[bid]
            shared = b['shared_acceptance']
            if not shared['plan_ref']:
                require(len(b['mapping_ids']) <= 1 or b['combination'] == 'unresolved', 'ECQ-SHARED-PLAN', bid, '共享组合缺原计划')
                parent_intervals[bid] = intervals[b['mapping_ids'][0]] if len(b['mapping_ids']) == 1 else None
                paths[bid] = None
                continue
            plan = ref(shared['plan_ref'])
            self.shape(plan, 'shared_plan', 'ECQ-SHARED-PLAN')
            require(plan['business_requirement_id'] == bid and plan['combination'] == b['combination'], 'ECQ-SHARED-COMBINATION', bid, '原计划组合不一致')
            require(set(plan['mapping_ids']) == set(b['mapping_ids']), 'ECQ-MAPPING', bid, '计划职责集合不一致')
            points = unique(plan['points'], 'id', bid)
            require(sum(fraction(p['share']) for p in points.values()) == 1, 'ECQ-SHARED-MATRIX', bid, '共同点份额和不为1')
            matrix = {}
            for r in shared['point_estimate_refs']:
                row = ref(r)
                self.shape(row, 'shared_point_estimate', 'ECQ-SHARED-MATRIX')
                k = (row['mapping_id'], row['point_id'])
                require(k not in matrix, 'ECQ-SHARED-MATRIX', bid, '重复矩阵单元')
                matrix[k] = row
                witness = ref(row['basis_ref'])
                require(all(witness.get(k) == row[k] for k in ['mapping_id', 'point_id', 'implemented']) and row['applies_to_version'] == scope['product_version'] == witness.get('version') and witness.get('environment') == scope['environment'], 'ECQ-BASIS', bid, '共同点依据不匹配')
                require(timepoint(row['assessed_at']) <= cutoff, 'ECQ-TIME', bid, '共同点估计晚于cutoff')
            require(set(matrix) == {(m, p) for m in b['mapping_ids'] for p in points}, 'ECQ-SHARED-MATRIX', bid, '共同点矩阵不完整')
            local = {}
            for mid in b['mapping_ids']:
                low = sum(fraction(p['share']) * (matrix[mid, pid]['implemented'] is True) for pid, p in points.items())
                high = sum(fraction(p['share']) * (matrix[mid, pid]['implemented'] is not False) for pid, p in points.items())
                local[mid] = (low, high)
                require(local[mid] == intervals[mid], 'ECQ-BASIS', mid, '局部份额与共同点矩阵不相等')
                if estimates[mid]['basis_ref']:
                    parts = {p['id']: p for p in ref(estimates[mid]['basis_ref'])['parts']}
                    require(set(parts) == set(points) and all(fraction(parts[pid]['share']) == fraction(points[pid]['share']) and (matrix[mid, pid]['implemented'] is None or parts[pid]['implemented_fraction'] == int(matrix[mid, pid]['implemented'])) for pid in points), 'ECQ-BASIS', mid, '局部验收份额与矩阵不一致')
            if b['combination'] == 'all_of':
                require(plan['selection_unit'] == 'all_roles_per_point', 'ECQ-SHARED-COMBINATION', bid, 'all_of必须逐点全部职责')
                parent_intervals[bid] = (sum(fraction(p['share']) * all(matrix[m, pid]['implemented'] is True for m in b['mapping_ids']) for pid, p in points.items()), sum(fraction(p['share']) * all(matrix[m, pid]['implemented'] is not False for m in b['mapping_ids']) for pid, p in points.items()))
                paths[bid] = None
            else:
                require(plan['selection_unit'] == 'whole_mapping_path', 'ECQ-SHARED-COMBINATION', bid, 'any_of必须完整路径')
                parent_intervals[bid] = (max(v[0] for v in local.values()), max(v[1] for v in local.values()))
                paths[bid] = True if any(v[0] == 1 for v in local.values()) else (None if any(v[1] == 1 for v in local.values()) else False)
        # Full archive checks run before contribution projections and source IDs never form event identity.
        session_rows = {}
        for task, link in sessions.items():
            row = ref(link['record_ref'])
            vals = {k: None if p is None else pointer(row, p) for k, p in link['fields'].items()}
            require(vals['id'] == link['session_id'], 'ECQ-SESSION-MAPPING', task, 'Session ID不匹配原记录')
            classification = 'unknown'
            try:
                asof = timepoint(vals['status_as_of']) if vals['status_as_of'] is not None else None
                started = timepoint(vals['started_at']) if vals['started_at'] is not None else None
                completed = timepoint(vals['completed_at']) if vals['completed_at'] is not None else None
                if trusted[link['record_ref']['source_id']] and asof == cutoff and started is not None and started <= cutoff:
                    if vals['status'] == 'done' and completed is not None and started <= completed <= cutoff:
                        classification = 'completed'
                    elif vals['status'] == 'doing' and completed is None:
                        classification = 'doing'
            except ECQError:
                pass  # Unparseable source metadata never creates a Session classification.
            session_rows[task] = {'session_id': link['session_id'], 'category': vals['category'], 'classification': classification,
                                  'record_ref': link['record_ref'], 'source_values': vals, 'trusted': trusted[link['record_ref']['source_id']]}
        events = {}
        for c in doc['contributions']:
            er, ir = c['event_ref'], c['item_ref']
            prefix = ir['pointer'] + '/events/'
            require(er['source_id'] == ir['source_id'] and er['source_id'] in archives and er['pointer'].startswith(prefix) and re.fullmatch(r'0|[1-9][0-9]*', er['pointer'][len(prefix):]), 'ECQ-EVENT', c['id'], '贡献必须定位所属item的真实事件')
            item, event = ref(ir), ref(er)
            index = int(er['pointer'][len(prefix):])
            require(timepoint(event['observed_at']) <= cutoff, 'ECQ-TIME', c['id'], '贡献事件晚于cutoff')
            require(event['stage'] in scope['stage_ids'], 'ECQ-EVENT', c['id'], '贡献事件不属于本scope纳入阶段')
            require(event['task_id'] in sessions, 'ECQ-SESSION-MAPPING', c['id'], '事件缺精确task到Session映射')
            for mid in c['mapping_ids']:
                require(mid in mappings and mappings[mid]['m1_id'] in item['m1_ids'] and set(mappings[mid]['coverage_requirement_ids']) <= set(item['requirement_ids']), 'ECQ-MAPPING', c['id'], '贡献职责不属于原子/item')
            identity = archives[er['source_id']]['identities'][(item['id'], index)]
            if identity in events:
                require(events[identity]['event'] == event, 'ECQ-HISTORY-PREFIX', c['id'], '逻辑事件内容冲突')
                events[identity]['contribution_ids'].append(c['id'])
                events[identity]['mapping_ids'] = sorted(set(events[identity]['mapping_ids']) | set(c['mapping_ids']))
                events[identity]['kinds'] = sorted(set(events[identity]['kinds']) | {c['kind']})
            else:
                events[identity] = {'identity': identity, 'item_id': item['id'], 'event_index': index, 'event': event,
                                    'contribution_ids': [c['id']], 'mapping_ids': list(c['mapping_ids']),
                                    'session_id': sessions[event['task_id']]['session_id'], 'kinds': [c['kind']]}
        conditions, covered = [], set()
        for check in doc['quality_checks']:
            bid = check['business_requirement_id']
            require(bid in ledger and bid in scope['requirement_ids'], 'ECQ-CONDITIONS', bid, '检查必须属于当前原要求')
            b = ledger[bid]
            cond = ref(check['condition_ref'])
            require(check['condition_ref'] in b['required_condition_refs'] and set(check['mapping_ids']) <= set(b['mapping_ids']), 'ECQ-CONDITIONS', bid, '条件/职责归属错误')
            for mid in check['mapping_ids']:
                pair = (mid, cond['id'])
                require(pair not in covered, 'ECQ-CONDITIONS', bid, '条件职责重复')
                covered.add(pair)
            result_ref = check['result_ref']
            vals, gaps = {}, []
            source_status, status, applicable, reused = 'unknown', 'unknown', False, False
            trust = False
            if result_ref is not None:
                row = ref(result_ref)
                vals = {k: pointer(row, p) for k, p in check['fields'].items()}
                require(vals['condition_id'] == cond['id'], 'ECQ-CONDITIONS', bid, '原结果条件ID不一致')
                source_status = vals['status']
                status = source_status if source_status in ('observed_pass', 'reported_pass', 'not_run', 'failed', 'blocked', 'unknown') else 'unknown'
                trust = trusted[result_ref['source_id']]
                applicable = len(b['mapping_ids']) == 1
                if check['applicability_ref'] is not None:
                    app = ref(check['applicability_ref'])
                    self.shape(app, 'quality_applicability', 'ECQ-Q-APPLICABILITY')
                    require(app['business_requirement_id'] == bid and app['condition_id'] == cond['id'] and app['result_ref'] == result_ref and set(app['mapping_ids']) <= set(b['mapping_ids']) and set(check['mapping_ids']) <= set(app['mapping_ids']), 'ECQ-Q-APPLICABILITY', bid, '结果适用职责不一致')
                    applicable = True
                if check['reuse_applicability_ref'] is not None:
                    reuse = ref(check['reuse_applicability_ref'])
                    self.shape(reuse, 'reuse_applicability', 'ECQ-REUSE-APPLICABILITY')
                    require(reuse['result_ref'] == result_ref and reuse['original_product_version'] == vals['version'] and reuse['original_environment'] == vals['environment'] and reuse['target_product_version'] == scope['product_version'] and reuse['target_environment'] == scope['environment'], 'ECQ-REUSE-APPLICABILITY', bid, '复用原/当前版本环境或结果错误')
                    require(vals['observed_at'] is not None and timepoint(vals['observed_at']) <= timepoint(reuse['reviewed_at']) <= cutoff, 'ECQ-REUSE-APPLICABILITY', bid, '复用时间不合法')
                    require(pointer(row, reuse['original_result_closure_pointer']) == reuse['original_closure_ref'], 'ECQ-REUSE-APPLICABILITY', bid, '原观察未保存所声称的原闭包绑定')
                    before, after = ref(reuse['original_closure_ref']), ref(reuse['current_closure_ref'])
                    for closure in (before, after):
                        self.shape(closure, 'artifact_closure', 'ECQ-REUSE-APPLICABILITY')
                        require(closure['product_id'] == doc['product_id'] and set(check['mapping_ids']) <= set(closure['mapping_ids']) <= set(b['mapping_ids']) and cond['id'] in closure['condition_ids'], 'ECQ-REUSE-APPLICABILITY', bid, '闭包产品/职责/条件错误')
                        art_keys = []
                        for artifact in closure['artifacts']:
                            data = self.binding(artifact, 'artifact-closure', False)
                            art_keys.append(artifact['path'])
                            # JSON dependency manifests are supported; arbitrary observed binary artifacts are hashed unchanged.
                            if artifact['path'].endswith('.json'):
                                value = strict_json(data, artifact['path'])
                                if isinstance(value, dict) and 'consumed_artifacts' in value:
                                    require(all(x in closure['artifacts'] for x in value['consumed_artifacts']), 'ECQ-REUSE-APPLICABILITY', bid, '闭包遗漏消费依赖')
                        require(len(art_keys) == len(set(art_keys)), 'ECQ-REUSE-APPLICABILITY', bid, '闭包重复artifact')
                    require(before['environment'] == reuse['original_environment'] and after['environment'] == reuse['target_environment'], 'ECQ-REUSE-APPLICABILITY', bid, '闭包环境与复用声明不符')
                    def closure_identity(v):
                        return (v['product_id'], v['environment'], tuple(sorted(v['mapping_ids'])), tuple(sorted(v['condition_ids'])), v['boundary_id'], tuple(sorted((a['path'], a['sha256']) for a in v['artifacts'])))
                    reused = reuse['closure_completeness'] == 'complete' and closure_identity(before) == closure_identity(after)
                    if not reused:
                        gaps.append('完整闭包或环境发生变化，不支持精确复用')
                if type(vals['independent']) is not bool:
                    status = 'unknown'
                    gaps.append('原independent字段不是布尔值')
                if not trust:
                    gaps.append('unverified：原文抽取未核对')
                if not applicable:
                    gaps.append('共享职责缺结果适用证明')
                if not trust or not applicable:
                    status = 'unknown'
                if vals['version'] is None or vals['environment'] is None:
                    status = 'unknown'
                    gaps.append('版本或环境未记录')
                elif source_status != 'not_run' and (vals['version'] != scope['product_version'] or vals['environment'] != scope['environment']) and not reused:
                    status = 'stale'
                    gaps.append('原观察不适用于当前版本或环境')
                try:
                    observed = timepoint(vals['observed_at']) if vals['observed_at'] is not None else None
                    expires = timepoint(vals['expires_at']) if vals['expires_at'] is not None else None
                    if observed is not None and observed > cutoff:
                        status = 'unknown'
                        gaps.append('观察晚于cutoff')
                    if expires is not None and cutoff > expires:
                        status = 'stale'
                        gaps.append('观察超过到期时间')
                    if source_status == 'observed_pass' and (observed is None or not isinstance(vals['observer'], str) or not vals['observer'].strip()):
                        status = 'unknown'
                        gaps.append('缺真实观察者或原观察时间')
                except ECQError:
                    status = 'unknown'
                    gaps.append('观察时间不可解析')
            else:
                require(check['applicability_ref'] is None and check['reuse_applicability_ref'] is None, 'ECQ-Q-APPLICABILITY', bid, '缺结果不能声明适用/复用')
                gaps.append('尚缺与本条件精确绑定的适用检查结果')
            conditions.append({'business_requirement_id': bid, 'mapping_ids': list(check['mapping_ids']), 'condition_id': cond['id'], 'condition_ref': check['condition_ref'], 'result_ref': result_ref, 'applicability_ref': check['applicability_ref'], 'source_values': vals, 'source_status': source_status, 'status': status, 'trusted': trust, 'applicable': applicable, 'reused': reused, 'reuse_applicability_ref': check['reuse_applicability_ref'], 'gaps': gaps})
        expected_pairs = {(mid, ref(cr)['id']) for bid in scope['requirement_ids'] for mid in ledger[bid]['mapping_ids'] for cr in ledger[bid]['required_condition_refs']}
        require(covered == expected_pairs, 'ECQ-CONDITIONS', '/quality_checks', '每职责必需条件未恰覆盖一次')
        issues = []
        issue_source = sources[doc['issues']['source_id']]
        for ip in doc['issues']['record_pointers']:
            row = pointer(raw[issue_source['id']], ip)
            vals = {k: pointer(row, p) for k, p in doc['issues']['fields'].items()}
            require(set(vals['m1_ids']) <= set(caps) and set(vals['business_requirement_ids']) <= set(ledger), 'ECQ-ISSUE', ip, '问题影响集合不存在')
            require(timepoint(vals['observed_at']) <= cutoff, 'ECQ-TIME', ip, '问题晚于cutoff')
            vals['record_ref'] = {'source_id': issue_source['id'], 'pointer': ip}
            issues.append(vals)
            if vals['status'] not in ('closed', 'resolved', 'fixed') and vals['invalidates_implementation']:
                for mid, m in mappings.items():
                    if m['m1_id'] in vals['m1_ids'] and m['business_requirement_id'] in vals['business_requirement_ids'] and mid in estimates:
                        e = estimates[mid]
                        require(e['low'] is None or e['high'] < 1, 'ECQ-IMPLEMENTATION-ISSUE', mid, '功能被否定而C未回退/未知')
        for n in steps.values():
            require(set(n['mapping_ids']) <= set(mappings) and all(i in steps for i in n['dependencies']), 'ECQ-NEXT-STEP', n['id'], '建议职责/依赖不存在')
            for mid in n['mapping_ids']:
                bid = mappings[mid]['business_requirement_id']
                require(any(r == ledger[bid]['definition_ref'] or (isinstance(ref(r), dict) and ref(r).get('id') in (mid, bid)) for r in n['input_refs']), 'ECQ-NEXT-STEP', n['id'], '建议输入不支持全部职责')
            if n['effort'] is not None:
                require(0 <= n['effort']['low'] <= n['effort']['high'], 'ECQ-NEXT-STEP', n['id'], '工作量区间错误')
        def visit(step, stack):
            require(step not in stack, 'ECQ-NEXT-STEP', step, '建议依赖循环')
            for dep in steps[step]['dependencies']:
                visit(dep, stack + (step,))
        for step in steps:
            visit(step, ())
        base = None
        change = ref(scope['scope_change_ref']) if scope['scope_change_ref'] else None
        if doc['base_assessment']:
            base_doc = self.binding(doc['base_assessment'], 'base-assessment')
            require(base_doc['product_id'] == doc['product_id'], 'ECQ-SCOPE-BASE', assessment, 'base产品不一致')
            require(timepoint(base_doc['cutoff']) < cutoff, 'ECQ-TIME', assessment, 'base cutoff未严格递增')
            bs = base_doc['scope']
            bmodel_binding = next(s['binding'] for s in base_doc['sources'] if s['id'] == bs['current_model_source_id'])
            bmodel = self.binding(bmodel_binding, 'base-model')
            br, cr = set(bs['requirement_ids']), set(scope['requirement_ids'])
            bm = {n['id'] for n in bmodel['nodes'] if n['node_kind'] == 'm1'}
            bw = {b['id']: b['weight'] for b in base_doc['business_requirements']}
            cw = {b['id']: b['weight'] for b in doc['business_requirements']}
            if any(bw[i] != cw[i] for i in br & cr) or bs['weight_policy'] != scope['weight_policy']:
                require(bs['weight_version'] != scope['weight_version'], 'ECQ-WEIGHT', '/scope/weight_version', '权重/政策改变必须使用新weight_version')
            expected_change = {'base_snapshot_id': base_doc['snapshot_id'], 'current_snapshot_id': doc['snapshot_id'],
                               'added_requirement_ids': sorted(cr - br), 'removed_requirement_ids': sorted(br - cr),
                               'added_m1_ids': sorted(current_m - bm), 'removed_m1_ids': sorted(bm - current_m),
                               'weight_changes': [{'requirement_id': i, 'before': bw[i], 'after': cw[i]} for i in sorted(br & cr) if bw[i] != cw[i]],
                               'before_model_binding': bmodel_binding, 'after_model_binding': sources[scope['current_model_source_id']]['binding'],
                               'before_ruleset': base_doc['ruleset']['id'], 'after_ruleset': doc['ruleset']['id']}
            for k in ['tp_ids', 'product_version', 'environment']:
                expected_change['before_' + k], expected_change['after_' + k] = bs[k], scope[k]
            changed = any(expected_change[k] for k in ['added_requirement_ids', 'removed_requirement_ids', 'added_m1_ids', 'removed_m1_ids', 'weight_changes']) or any(expected_change['before_' + k] != expected_change['after_' + k] for k in ['tp_ids', 'product_version', 'environment', 'model_binding', 'ruleset']) or bs['weight_policy'] != scope['weight_policy'] or bs['weight_version'] != scope['weight_version']
            require(change is not None or not changed, 'ECQ-SCOPE-DELTA', assessment, '口径变化缺直接base记录')
            if change:
                for k, v in expected_change.items():
                    actual = change.get(k)
                    ok = set(actual) == set(v) if k in ('added_requirement_ids', 'removed_requirement_ids', 'added_m1_ids', 'removed_m1_ids', 'before_tp_ids', 'after_tp_ids') else actual == v
                    require(ok, 'ECQ-SCOPE-DELTA', '/scope/scope_change_ref/' + k, '直接base差异不一致')
            base = self.evaluate(doc['base_assessment']['path'], chain + (assessment,))
            require(base['snapshot_id'] != doc['snapshot_id'] and doc['snapshot_id'] not in {h['snapshot_id'] for h in base['history']}, 'ECQ-SCOPE-BASE', assessment, 'assessment snapshot重复')
        else:
            require(change is None, 'ECQ-SCOPE-BASE', '/scope/scope_change_ref', '无直接base不能登记本轮差异')
            changed = False
        # Metrics below consume only validated source facts; no observer state or Coverage inference.
        def c_metric(bids, local_m=None, inactive=False):
            bids = list(bids)
            weight = sum(fraction(ledger[bid]['weight']) for bid in bids)
            vals = {}
            mids = []
            for bid in bids:
                selected = [mid for mid in ledger[bid]['mapping_ids'] if local_m is None or mappings[mid]['m1_id'] == local_m]
                mids.extend(selected)
                if local_m is None:
                    vals[bid] = parent_intervals[bid]
                elif len(selected) == 1:
                    vals[bid] = intervals[selected[0]]
                else:
                    # Several duties on the same M still retain one original requirement weight.
                    vals[bid] = parent_intervals[bid] if set(selected) == set(ledger[bid]['mapping_ids']) else None
            unknown = sum(all(estimates[mid]['low'] is None for mid in ledger[bid]['mapping_ids'] if mid in estimates and (local_m is None or mappings[mid]['m1_id'] == local_m)) for bid in bids)
            interval, exact = None, (None, None)
            if not inactive and weight and all(v is not None for v in vals.values()):
                exact = tuple(sum(fraction(ledger[bid]['weight']) * vals[bid][i] for bid in bids) / weight for i in (0, 1))
                interval = [float(x) for x in exact]
            core_states = [estimates[mid]['core_behavior'] for mid in mids if mid in estimates]
            core = 'unavailable' if 'known_unavailable' in core_states else ('unknown' if 'unknown' in core_states or not core_states else 'available')
            low, high = interval if interval is not None else (None, None)
            complete = scope['requirements_completeness'] == 'complete'
            label = ('本范围共' if complete else '仅针对已整理的') + str(len(bids)) + '条要求'
            return {'interval': interval, 'low': low, 'high': high, 'denominator_weight': float(weight), 'requirement_count': len(bids), 'unknown_count': unknown, 'whole_M_claim_allowed': complete and not inactive, 'scope_label': label, 'display': display(exact[0], exact[1], bool(bids) and unknown == len(bids), core), 'core_behavior': core}
        priority = ['failed', 'blocked', 'stale', 'unknown', 'not_run', 'reported_pass', 'observed_pass']
        def q_metric(bids, local_m=None):
            bids = list(bids)
            relevant = [c for c in conditions if c['business_requirement_id'] in bids and (local_m is None or any(mappings[mid]['m1_id'] == local_m for mid in c['mapping_ids']))]
            reqs = []
            for bid in bids:
                if not ledger[bid]['critical'] or not ledger[bid]['required_condition_refs']:
                    continue
                rows = [c for c in relevant if c['business_requirement_id'] == bid]
                statuses = [c['status'] for c in rows] or ['unknown']
                status = min(statuses, key=priority.index)
                reqs.append({'id': bid, 'status': status, 'observed': status == 'observed_pass'})
            matching_issues = [i for i in issues if set(i['business_requirement_ids']) & set(bids) and (local_m is None or local_m in i['m1_ids'])]
            counts = dict(Counter(r['status'] for r in reqs))
            blocked = counts.get('failed', 0) > 0 or any(i['status'] not in ('closed', 'resolved', 'fixed') and (i['blocking'] or i['severity'] in ('critical', 'blocking')) for i in matching_issues)
            quality_complete = all(sources[c['result_ref']['source_id']]['completeness'] == 'complete' for c in relevant if c['result_ref'])
            sources_complete = quality_complete and scope['checks_completeness'] == scope['issues_completeness'] == scope['requirements_completeness'] == 'complete' and issue_source['availability'] == 'connected' and issue_source['completeness'] == 'complete'
            if blocked:
                status = 'acceptance_blocked'
            elif not sources_complete or not reqs:
                status = 'insufficient_evidence'
            elif all(r['observed'] for r in reqs):
                status = 'no_blocker_observed'
            else:
                status = 'pending_verification'
            return {'observed': [sum(r['observed'] for r in reqs), len(reqs)], 'status': status, 'conditions': relevant, 'requirements': reqs, 'status_counts': counts, 'issues': matching_issues, 'issue_count': len(matching_issues) if scope['issues_completeness'] == issue_source['completeness'] == 'complete' and issue_source['availability'] == 'connected' else None}
        def e_metric(local_m=None):
            selected = [e for e in events.values() if local_m is None or any(mappings[mid]['m1_id'] == local_m for mid in e['mapping_ids'])]
            ids = sorted({e['session_id'] for e in selected})
            rows = list({r['session_id']: r for r in session_rows.values() if r['session_id'] in ids}.values())
            connected = any(s['kind'] == 'progress' and s['availability'] == 'connected' for s in sources.values()) and scope['session_history_completeness'] != 'unknown'
            count = len(ids) if connected else None
            return {'count': count, 'history_completeness': scope['session_history_completeness'], 'session_ids': ids, 'sessions': rows, 'event_count': len(selected) if connected else None, 'events': selected,
                    'outcome_counts': dict(Counter(e['event']['outcome'] for e in selected)), 'category_counts': dict(Counter(r['category'] or 'unknown' for r in rows)),
                    'planned_sessions': list({r['session_id']: r for r in session_rows.values() if r['source_values']['status'] in ('planned', 'todo') and r['session_id'] not in {e['session_id'] for e in events.values()}}.values()),
                    'completed_session_count': sum(r['classification'] == 'completed' for r in rows), 'doing_session_count': sum(r['classification'] == 'doing' for r in rows), 'classification_unknown_count': sum(r['classification'] == 'unknown' for r in rows)}
        requirements = []
        for bid in scope['requirement_ids']:
            b = ledger[bid]
            requirements.append({'id': bid, 'definition_ref': b['definition_ref'], 'definition': ref(b['definition_ref']), 'weight': b['weight'], 'critical': b['critical'], 'mapping_ids': b['mapping_ids'], 'C': c_metric([bid]), 'Q': q_metric([bid]), 'complete_path_available': paths[bid]})
        capabilities = []
        for mid, cap in caps.items():
            bids = [bid for bid in scope['requirement_ids'] if any(mappings[m]['m1_id'] == mid for m in ledger[bid]['mapping_ids'])] if mid in current_m else []
            local_requirements = []
            for bid in bids:
                b = ledger[bid]
                local_requirements.append({'id': bid, 'definition_ref': b['definition_ref'], 'definition': ref(b['definition_ref']),
                                           'weight': b['weight'], 'critical': b['critical'],
                                           'mapping_ids': [mapping_id for mapping_id in b['mapping_ids'] if mappings[mapping_id]['m1_id'] == mid],
                                           'C': c_metric([bid], mid), 'Q': q_metric([bid], mid)})
            capabilities.append({'m1_id': mid, 'current': mid in current_m, 'definition_ref': cap['definition_ref'], 'definition': ref(cap['definition_ref']), 'E': e_metric(mid), 'C': c_metric(bids, mid, inactive=mid not in current_m), 'Q': q_metric(bids, mid), 'requirement_ids': bids, 'requirements': local_requirements, 'next_step_ids': [n['id'] for n in steps.values() if any(mappings[m]['m1_id'] == mid for m in n['mapping_ids'])]})
        mapping_rows = []
        for mid, m in mappings.items():
            e = estimates.get(mid)
            iv = intervals.get(mid)
            mapping_rows.append({'id': mid, 'business_requirement_id': m['business_requirement_id'], 'm1_id': m['m1_id'], 'resolution': m['resolution'], 'relation': m['relation'], 'definition': m, 'estimate': dict(e, basis=ref(e['basis_ref']) if e['basis_ref'] else None) if e else None, 'C': {'interval': [float(x) for x in iv] if iv else None, 'low': float(iv[0]) if iv else None, 'high': float(iv[1]) if iv else None, 'display': display(iv[0] if iv else None, iv[1] if iv else None, e is None or e['low'] is None, 'unavailable' if e and e['core_behavior'] == 'known_unavailable' else ('unknown' if not e or e['core_behavior'] == 'unknown' else 'available'))}, 'Q': q_metric([m['business_requirement_id']], m['m1_id']) if mid in active else q_metric([], m['m1_id'])})
        global_metrics = {'E': e_metric(), 'C': c_metric(scope['requirement_ids']), 'Q': q_metric(scope['requirement_ids'])}
        comparison = None
        if base:
            def delta(before, after, comparable):
                return [100 * (after['C']['interval'][i] - before['C']['interval'][i]) for i in (0, 1)] if comparable and before and after and before['C']['interval'] is not None and after['C']['interval'] is not None else None
            cap_changes = []
            old_caps = {c['m1_id']: c for c in base['capabilities']}
            new_caps = {c['m1_id']: c for c in capabilities}
            for mid in sorted(set(old_caps) | set(new_caps)):
                before, after = old_caps.get(mid), new_caps.get(mid)
                comparable = not changed and before is not None and after is not None and before['current'] == after['current']
                old_local = {r['id']: r for r in before['requirements']} if before else {}
                new_local = {r['id']: r for r in after['requirements']} if after else {}
                local_changes = []
                for bid in sorted(set(old_local) | set(new_local)):
                    previous, current = old_local.get(bid), new_local.get(bid)
                    requirement_comparable = comparable and previous is not None and current is not None
                    local_changes.append({'id': bid, 'before': previous, 'after': current,
                                          'change_kind': 'added' if previous is None else ('removed' if current is None else 'retained'),
                                          'comparable': requirement_comparable,
                                          'implementation_delta_pp': delta(previous, current, requirement_comparable)})
                cap_changes.append({'m1_id': mid, 'before': {k: before[k] for k in ('current', 'E', 'C', 'Q', 'requirement_ids', 'requirements')} if before else None, 'after': {k: after[k] for k in ('current', 'E', 'C', 'Q', 'requirement_ids', 'requirements')} if after else None, 'requirement_changes': local_changes, 'comparable': comparable, 'implementation_delta_pp': delta(before, after, comparable), 'new_session_ids': sorted(set(after['E']['session_ids'] if after else []) - set(before['E']['session_ids'] if before else [])), 'removed_session_ids': sorted(set(before['E']['session_ids'] if before else []) - set(after['E']['session_ids'] if after else []))})
            req_changes = []
            old_reqs = {r['id']: r for r in base['requirements']}
            new_reqs = {r['id']: r for r in requirements}
            for bid in sorted(set(old_reqs) | set(new_reqs)):
                before, after = old_reqs.get(bid), new_reqs.get(bid)
                comparable = not changed and before is not None and after is not None
                req_changes.append({'id': bid, 'before': {k: before[k] for k in ('weight', 'mapping_ids', 'C', 'Q')} if before else None, 'after': {k: after[k] for k in ('weight', 'mapping_ids', 'C', 'Q')} if after else None, 'comparable': comparable, 'implementation_delta_pp': delta(before, after, comparable)})
            comparison = {'base_snapshot_id': base['snapshot_id'], 'before_global_C': base['global']['C'], 'after_global_C': global_metrics['C'], 'scope_change': change, 'comparable': not changed, 'implementation_delta_pp': delta(base['global'], global_metrics, not changed), 'capabilities': cap_changes, 'requirement_changes': req_changes}
        result = {'schema': 'ecq.projection.v1', 'lifecycle': 'candidate_not_approved', 'product_id': doc['product_id'], 'snapshot_id': doc['snapshot_id'], 'cutoff': doc['cutoff'], 'scope': scope,
                  'sources': [dict(s, trusted=trusted.get(s['id'], False)) for s in doc['sources']], 'bindings': [],
                  'global': global_metrics, 'capabilities': capabilities, 'requirements': requirements, 'mappings': mapping_rows, 'comparison': comparison, 'next_steps': doc['next_steps'], 'history': (base['history'] if base else []) + [{'snapshot_id': doc['snapshot_id'], 'cutoff': doc['cutoff'], 'current_m1_ids': sorted(current_m), 'global_C': global_metrics['C'], 'global_E': global_metrics['E']}], 'warnings': [], 'tp_target': tp_target, 'source_validation_reports': [{'source_id': sid, 'binding': sources[sid]['binding'], 'report': value} for sid, value in raw.items() if sources[sid]['kind'] == 'validation_report'], 'source_contexts': (base['source_contexts'] if base else []) + [{'snapshot_id': doc['snapshot_id'], 'assessment': {'base': 'workspace', 'path': assessment, 'sha256': digest(self.path(assessment).read_bytes())}, 'sources': [dict(s, trusted=trusted.get(s['id'], False)) for s in doc['sources']]}]}
        self.assessment_cache[assessment] = result
        return result


def evaluate(workspace, assessment):
    """Validate all consumed bytes and semantic relations before returning pure data."""
    try:
        ctx = Context(workspace)
        result = ctx.evaluate(str(assessment))
    except OSError as exc:
        raise ECQError('ECQ-IO', str(assessment), str(exc)) from exc
    except ECQError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # Source documents may be syntactically valid JSON but have an invalid semantic shape.
        raise ECQError('ECQ-SOURCE-SHAPE', str(assessment), str(exc)) from exc
    result['bindings'] = sorted(ctx.bindings.values(), key=lambda b: (b.get('base', 'workspace'), b['path'], b['role']))
    for b in result['bindings']:
        b.setdefault('base', 'workspace')
    result['warnings'] = ctx.warnings
    canonical(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读候选ECQ校验/投影，不创建批准')
    parser.add_argument('command', choices=['validate', 'project'])
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--assessment', required=True)
    parser.add_argument('--output-dir')
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.workspace, args.assessment)
        if args.command == 'project':
            require(args.output_dir is not None, 'ECQ-PATH', '--output-dir', 'project必须提供新输出目录')
            workspace = Path(args.workspace).resolve()
            requested = Path(args.output_dir)
            require(not requested.is_absolute() and '..' not in requested.parts, 'ECQ-PATH', args.output_dir, '输出目录必须为workspace安全相对新路径')
            output = (workspace / requested).resolve()
            require(not output.exists() and output != workspace, 'ECQ-OUTPUT-EXISTS', args.output_dir, '禁止覆盖现有目录')
            try:
                output.relative_to(workspace)
            except ValueError as exc:
                raise ECQError('ECQ-PATH', args.output_dir, '输出越过workspace') from exc
            require(output.parent.is_dir(), 'ECQ-PATH', args.output_dir, '输出父目录不存在')
            temp = Path(tempfile.mkdtemp(prefix='.ecq-', dir=str(output.parent)))
            try:
                (temp / 'ecq-projection.json').write_bytes(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode() + b'\n')
                audit = {'schema': 'ecq.audit.v1', 'verdict': 'pass', 'lifecycle': result['lifecycle'], 'bindings': result['bindings'], 'claim_ceiling': '候选确定性投影；非独立验证、业务批准或Goal完成'}
                (temp / 'ecq-audit.json').write_bytes(json.dumps(audit, ensure_ascii=False, indent=2).encode() + b'\n')
                temp.rename(output)
            finally:
                if temp.exists():
                    shutil.rmtree(temp)
        print(json.dumps({'verdict': 'pass', 'snapshot_id': result['snapshot_id'], 'lifecycle': result['lifecycle']}, ensure_ascii=False))
        return 0
    except (ECQError, OSError) as exc:
        error = exc.as_dict() if isinstance(exc, ECQError) else {'code': 'ECQ-IO', 'path': args.assessment, 'message': str(exc)}
        print(json.dumps({'verdict': 'fail', 'error': error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
