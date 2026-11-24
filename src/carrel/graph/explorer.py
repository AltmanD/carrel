"""Atomic, write-revision driven projections for bounded graph navigation."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import asdict
import json
import os
from threading import RLock

from carrel.coverage import paper_coverage, missing_capabilities
from carrel.profile import PRESETS
from carrel.graph.build import _paper_node, revision_of
from carrel.graph.identity import AliasIndex
from carrel.graph.labels import paper_label
from carrel.graph.display import branch_names
from carrel.graph.model import Graph, Edge, Node
from carrel.graph.revision import revision, paper_revision
from carrel.store.paths import paper_dir


def values(value):
    return sorted(set([value] if isinstance(value, str) and value else value or []))


class Explorer:
    def __init__(self, service):
        self.service = service
        self.lock = RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='graph-projection')
        self.snapshots = {}
        self.pending = {}
        self.errors = {}
        self.failed_tokens = {}
        self.epoch = 0
        self.closed = False
        self.paper_cache = {}
        self.paper_generations = {}
        self.cache_path = service.data_dir / '.graph-projection-v2.json'
        try:
            data = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if data['version'] == 2:
                snapshot = data['snapshot']
                snapshot['nodes'] = {key: Node(**value) for key, value in snapshot['nodes'].items()}
                snapshot['ranks'] = {tuple(key): value for key, value in snapshot['ranks']}
                snapshot['token'] = None
                self.snapshots['graph'] = snapshot
                self.paper_cache = data['papers']
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def persist(self, snapshot):
        data = {key: value for key, value in snapshot.items() if key != 'tree'}
        data['nodes'] = {key: asdict(value) for key, value in data['nodes'].items()}
        data['ranks'] = [[list(key), value] for key, value in data['ranks'].items()]
        temporary = self.cache_path.with_suffix('.tmp')
        try:
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(dict(version=2, snapshot=data, papers=self.paper_cache), ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, self.cache_path)
        except OSError:
            pass

    def paper_stamp(self, paper, base):
        files = []
        # Background validation only; hot requests use the global revision.
        if base.exists():
            candidates = [base / name for name in ('doc.json', 'sections.json', 'summary.json', 'preview.json',
                'classification.json', 'assessment.json', 'motivation.json', 'glossary.json', 'title.json',
                'graph_display.json', 'relations.json', 'extract_skipped.json', 'translate_warnings.json')]
            candidates += list((base / 'modules').glob('*.json')) + list((base / 'translation').glob('*.json'))
            for path in sorted(candidates):
                if path.is_file():
                    stat = path.stat()
                    files.append((str(path.relative_to(base)), stat.st_mtime_ns, stat.st_size))
        taxonomy = self.service.data_dir / 'taxonomy.toml'
        stamp = taxonomy.stat() if taxonomy.exists() else None
        return revision_of([paper, files, (stamp.st_mtime_ns, stamp.st_size) if stamp else None])

    def token(self):
        return (self.service.db.graph_revision, revision(self.service.data_dir), self.epoch)

    def invalidate(self):
        with self.lock:
            self.epoch += 1

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)

    def refresh(self):
        self.invalidate()
        self.ensure('graph')
        return {'status': 'building', 'retry_after_ms': 250}

    def ensure(self, view):
        with self.lock:
            token = self.token()
            snapshot = self.snapshots.get(view)
            if (not self.closed and (not snapshot or snapshot['token'] != token)
                    and view not in self.pending and self.failed_tokens.get(view) != token):
                self.pending[view] = True
                self.pool.submit(self.build, view, token)
            return snapshot, view in self.pending

    def build(self, view, token):
        try:
            snapshot = self.project(token)
            if view == 'tree':
                snapshot['tree'] = self.service.motivation_tree()
            if token == self.token():
                self.persist(snapshot)
            with self.lock:
                if token == self.token():
                    self.snapshots[view] = snapshot
                    self.errors.pop(view, None)
                    self.failed_tokens.pop(view, None)
        except Exception as exc:
            with self.lock:
                self.errors[view] = str(exc)
                self.failed_tokens[view] = token
        finally:
            with self.lock:
                self.pending.pop(view, None)

    def project(self, token):
        service = self.service
        papers = {p['id']: dict(p) for p in service.db.list_papers()}
        tags = defaultdict(list)
        for row in service.db.tag_rows():
            tags[row['paper_id']].append(row['tag'])
        collections = {c['id']: dict(c) for c in service.db.collections()}
        memberships = defaultdict(list)
        ranks = {}
        for row in service.db.query('SELECT * FROM paper_collections'):
            if row['collection_id'] in collections:
                memberships[row['paper_id']].append(collections[row['collection_id']])
                ranks[(row['paper_id'], row['collection_id'])] = row['rank'] if row['rank'] is not None else 2147483647
        nodes, categories, coverage, motivations = {}, {}, {}, {}
        for pid, paper in papers.items():
            base = paper_dir(service.data_dir, pid)
            cached = self.paper_cache.get(pid)
            generation = revision_of([paper, paper_revision(service.data_dir, base.name), self.epoch])
            stamp = cached['stamp'] if cached and self.paper_generations.get(pid) == generation else self.paper_stamp(paper, base)
            self.paper_generations[pid] = generation
            if cached and cached['stamp'] == stamp:
                node = Node(**deepcopy(cached['node']))
                cats = cached['categories']
                categories[pid] = cats
                coverage[pid] = cached['coverage']
                motivations[pid] = cached['motivation']
            else:
                motivations[pid] = service._read_json_cached(base / 'motivation.json')
                paths = service.categories_for(paper)
                if not paths and paper.get('discipline'):
                    paths = [paper]
                cats = []
                for path in paths:
                    parent = path.get('discipline') or path.get('level1') or ''
                    label = path.get('field') or path.get('level2') or parent or '未分类'
                    key = path.get('id') or path.get('category_id') or ' / '.join(filter(None, (parent, label if label != parent else ''))) or 'unclassified'
                    cats.append({'id': key, 'label': label, 'parent': parent, 'primary': bool(path.get('primary'))})
                cats = cats or [{'id': 'unclassified', 'label': '未分类', 'parent': ''}]
                cats.sort(key=lambda c: (not c.get('primary', False), c['id']))
                categories[pid] = cats
                caps = paper_coverage(base)
                coverage[pid] = next((depth for depth in ('full', 'notes', 'skim')
                                      if not missing_capabilities(PRESETS[depth], caps)), 'indexed')
                from carrel.titles import project_title
                node = _paper_node(project_title(paper, base, legacy_check=False), '')
                node.label = str(service._read_json_cached(base / 'graph_display.json').get('label') or
                                 paper_label(paper.get('title', ''), node.title,
                                             service._read_json_cached(base / 'glossary.json'),
                                             service._read_json_cached(base / 'summary.json')))
                self.paper_cache[pid] = dict(stamp=stamp, node=asdict(node), categories=cats,
                                             coverage=coverage[pid], motivation=motivations[pid])
            member = sorted(memberships[pid], key=lambda c: (c.get('sort') or 0, c['id']))
            node.meta.update(categories=cats, tags=tags[pid], coverage=coverage[pid],
                             collections=[{'id': c['id'], 'name': c['name']} for c in member],
                             branch={'key': cats[0]['id'], 'label': cats[0]['label']})
            nodes[pid] = node
        self.paper_cache = {pid: value for pid, value in self.paper_cache.items() if pid in papers}
        tasks = defaultdict(Counter)
        for job in service.db.query('SELECT paper_id, status FROM jobs WHERE rowid IN '
                                    '(SELECT MAX(rowid) FROM jobs GROUP BY paper_id, stage)'):
            if job['status'] in ('queued', 'running', 'pending', 'retry'):
                tasks[job['paper_id']]['pending'] += 1
            elif job['status'] in ('failed', 'error'):
                tasks[job['paper_id']]['failed'] += 1
        alias = AliasIndex.from_papers(papers.values())
        relations = {}
        for raw in service.db.query('SELECT * FROM relations ORDER BY src_id, kind, rank, dst_id'):
            row = dict(raw)
            source, target = row['src_id'], alias.local_paper(row)
            kind = row['kind']
            if source not in papers or not target or source == target or kind not in ('cites', 'cited_by', 'similar'):
                continue
            if kind == 'cited_by':
                source, target, kind = target, source, 'cites'
            if kind == 'similar':
                source, target = sorted((source, target))
            key = kind, source, target
            record = relations.setdefault(key, {'source': source, 'target': target, 'role': kind, 'evidence': [], 'rank': row.get('rank') or 0})
            record['evidence'].append(row)
            record['rank'] = min(record['rank'], row.get('rank') or 0)
        adjacency = defaultdict(list)
        for edge in relations.values():
            adjacency[edge['source']].append(edge)
            adjacency[edge['target']].append(edge)
        names = branch_names(service.data_dir)
        content_revision = revision_of([token, names, [(pid, entry['stamp']) for pid, entry in sorted(self.paper_cache.items())]])
        return dict(token=token, revision=content_revision, generated_at=datetime.now(timezone.utc).isoformat(),
                    papers=papers, nodes=nodes, categories=categories, coverage=coverage, tags=tags,
                    collections=collections, ranks=ranks, adjacency=adjacency, motivations=motivations, tasks=tasks,
                    branch_names=names)

    def select(self, snapshot, filters):
        selected = []
        for pid, node in snapshot['nodes'].items():
            cats = snapshot['categories'][pid]
            category_keys = {v for c in cats for v in (c['id'], c['label'], c['parent'])}
            collection_keys = {c['id'] for c in node.meta['collections']} or {'uncollected'}
            if filters['category'] and not category_keys.intersection(filters['category']):
                continue
            if filters['collection'] and not collection_keys.intersection(filters['collection']):
                continue
            keywords = set(filters['keyword'])
            tags = set(snapshot['tags'][pid])
            if keywords and not (keywords <= tags if filters['keywords_match'] == 'all' else keywords & tags):
                continue
            if filters['q'] and filters['q'].casefold() not in (node.title + ' ' + node.label + ' ' + ' '.join(tags)).casefold():
                continue
            selected.append(pid)
        return selected

    def ordered(self, snapshot, ids, sort, collection=''):
        if sort == 'name':
            return sorted(ids, key=lambda pid: (snapshot['nodes'][pid].label.casefold(), pid))
        if sort == 'depth':
            return sorted(ids, key=lambda pid: (-('indexed', 'skim', 'notes', 'full').index(snapshot['coverage'][pid]), pid))
        if collection:
            return sorted(ids, key=lambda pid: (snapshot['ranks'].get((pid, collection), 2147483647), pid))
        return sorted(ids, key=lambda pid: (str(snapshot['papers'][pid].get('created_at') or ''), pid), reverse=True)

    def card(self, snapshot, key, label, ids, kind, parent=''):
        representatives = self.ordered(snapshot, ids, 'recent', key if kind == 'collection' else '')[:3]
        return dict(id=key, label=label, parent=parent, count=len(ids), kind=kind,
                    coverage={depth: sum(snapshot['coverage'][pid] == depth for pid in ids) for depth in PRESETS},
                    representatives=[dict(id=pid, label=snapshot['nodes'][pid].label, title=snapshot['nodes'][pid].title,
                                          url=snapshot['nodes'][pid].url) for pid in representatives],
                    keywords=[tag for tag, _ in Counter(t for pid in ids for t in snapshot['tags'][pid]).most_common(5)])

    def explore(self, *, view='graph', mode='overview', group_by='category', group_id='', focus='', paper_id='',
                category=(), collection=(), keyword=(), keywords_match='any', q='', sort='recent',
                limit=None, offset=0, include_similar=False, include_context=False,
                include_cites=False, edge_limit=40, **kwargs):
        limit = max(1, min(200, int(limit or {'neighbors': 5, 'branch': 12}.get(mode, 24))))
        offset = max(0, int(offset))
        filters = dict(category=values(category), collection=values(collection), keyword=values(keyword),
                       keywords_match=keywords_match, q=q, sort=sort, group_by=group_by, group_id=group_id)
        snapshot, rebuilding = self.ensure(view)
        result = dict(status='ready' if snapshot else ('error' if view in self.errors else 'building'),
                      revision=snapshot['revision'] if snapshot else '', generated_at=snapshot['generated_at'] if snapshot else '',
                      view=view, mode=mode, scope='library' if view == 'graph' else 'motivation_tree',
                      filters=filters, total=0, shown=0, omitted=0, stats={}, notes=[], rebuilding=rebuilding, stale=bool(snapshot and rebuilding))
        if not snapshot:
            result.update(retry_after_ms=250, error=self.errors.get(view, ''))
            return result
        if view in self.errors:
            result['notes'].append(self.errors[view])
            result.update(stale=True, refresh_error=self.errors[view])
        ids = self.select(snapshot, filters)
        result['stats'] = {'library_total': len(snapshot['nodes']), 'coverage': self.card(snapshot, '', '', ids, '')['coverage']}
        result['stats']['tasks'] = {state: sum(snapshot.get('tasks', {}).get(pid, {}).get(state, 0) for pid in ids) for state in ('pending', 'failed')}
        if view == 'tree':
            return self.tree_response(snapshot, result, ids, mode, group_id, focus or paper_id, limit, offset, include_context)
        buckets = defaultdict(list)
        metadata = {}
        if group_by == 'collection':
            for cid, row in snapshot['collections'].items():
                if not filters['collection'] or cid in filters['collection']:
                    buckets[cid] = []
                    metadata[cid] = (row['name'], '')
        for pid in ids:
            node = snapshot['nodes'][pid]
            groups = node.meta['collections'] if group_by == 'collection' else snapshot['categories'][pid]
            if not groups:
                groups = [{'id': 'uncollected', 'name': '未加入集合'}]
            for group in groups:
                buckets[group['id']].append(pid)
                metadata[group['id']] = (group.get('label') or group.get('name'), group.get('parent', ''))
        if mode == 'overview':
            result['groups'] = [self.card(snapshot, key, metadata[key][0], members, group_by, metadata[key][1])
                                for key, members in sorted(buckets.items())]
            if sort == 'name':
                result['groups'].sort(key=lambda g: (g['label'].casefold(), g['id']))
            elif sort == 'depth':
                result['groups'].sort(key=lambda g: (-sum(g['coverage'].get(k, 0) * v for k, v in {'full':3,'notes':2,'skim':1}.items()), g['id']))
            elif group_by == 'collection':
                result['groups'].sort(key=lambda g: (snapshot['collections'].get(g['id'], {}).get('sort') or 0, g['id']))
            else:
                result['groups'].sort(key=lambda g: max((str(snapshot['papers'][p].get('created_at') or '') for p in buckets[g['id']]), default=''), reverse=True)
            result['facets'] = self.facets(snapshot)
            result.update(total=len(ids), shown=len(ids), omitted=0)
            if group_by == 'collection':
                result['notes'].append('集合允许重叠，各组数量之和可能大于去重总数。')
            return result
        scope = set(buckets.get(group_id, [])) if group_id else set(ids)
        result['scope_label'] = metadata.get(group_id, ('当前筛选范围' if any(filters[k] for k in ('category','collection','keyword','q')) else '全部论文', ''))[0]
        chosen = self.ordered(snapshot, scope, sort, group_id if group_by == 'collection' else '')[offset:offset + limit]
        edges = []
        focus = focus or paper_id
        if mode == 'neighbors':
            candidates = [e for e in snapshot['adjacency'].get(focus, []) if include_similar or e['role'] != 'similar']
            candidates.sort(key=lambda e: (e['role'] == 'similar', (e['target'] if e['source'] == focus else e['source']) not in scope, self.provider_rank(e), e['source'], e['target']))
            neighbors = list(dict.fromkeys(e['target'] if e['source'] == focus else e['source'] for e in candidates))
            chosen = ([focus] if focus in snapshot['nodes'] else []) + neighbors[:limit]
            edges = [e for e in candidates if e['source'] in chosen and e['target'] in chosen]
            result.update(focus=focus, neighbor_total=len(neighbors))
            total = len(neighbors) + int(focus in snapshot['nodes'])
        else:
            total = len(scope)
        if mode != 'neighbors' and (include_cites or include_similar):
            unique = {}
            for pid in chosen:
                for edge in snapshot['adjacency'].get(pid, []):
                    if (edge['source'] in chosen and edge['target'] in chosen
                            and (include_cites if edge['role'] == 'cites' else include_similar)):
                        unique[(edge['role'], edge['source'], edge['target'])] = edge
            edges = [unique[key] for key in sorted(unique)]
        edge_total = len(edges)
        edges = edges[:max(0, min(2000, int(edge_limit)))]
        result.update(edge_total=edge_total, edge_shown=len(edges), edge_omitted=edge_total - len(edges))
        graph = Graph(scope='library', revision=snapshot['revision'])
        graph.focus = snapshot['nodes'][focus].id if focus in snapshot['nodes'] else ''
        graph.nodes = [deepcopy(snapshot['nodes'][pid]) for pid in chosen]
        for node in graph.nodes:
            node.focus = node.meta['paper_id'] == focus
            node.meta['out_of_scope'] = node.meta['paper_id'] not in scope
            if group_by == 'collection':
                member = node.meta['collections']
                preferred = filters['collection'][0] if len(filters['collection']) == 1 else group_id
                main = next((c for c in member if c['id'] == preferred), member[0] if member else {'id': 'uncollected', 'name': '未加入集合'})
                node.meta['branch'] = {'key': main['id'], 'label': main['name']}
        graph.edges = [Edge(snapshot['nodes'][e['source']].id, snapshot['nodes'][e['target']].id, e['role']) for e in edges]
        result.update(graph=graph.to_dict(), total=total, shown=len(chosen), omitted=max(0, total - len(chosen)), offset=offset, next_offset=offset + len(chosen))
        return result

    @staticmethod
    def provider_rank(edge):
        return min(((row.get('provider') or '', row.get('rank') or 0)
                    for row in edge['evidence']), default=('', 0))

    def facets(self, snapshot):
        cats, collections, keywords = {}, Counter(), Counter()
        for pid, node in snapshot['nodes'].items():
            parents = set()
            for c in snapshot['categories'][pid]:
                facet = cats.setdefault(c['id'], {**c, 'level': 2 if c['parent'] else 1, 'count': 0})
                facet['count'] += 1
                if c['parent'] and c['parent'] != c['id']:
                    parents.add(c['parent'])
            for parent in parents:
                facet = cats.setdefault(parent, {'id': parent, 'label': parent, 'parent': '', 'level': 1, 'count': 0})
                facet['count'] += 1
            collections.update(c['id'] for c in node.meta['collections'])
            if not node.meta['collections']:
                collections['uncollected'] += 1
            keywords.update(set(snapshot['tags'][pid]))
        return dict(categories=list(cats.values()),
                    collections=[{'id': cid, 'label': c['name'], 'count': collections[cid]} for cid, c in snapshot['collections'].items()] +
                                [{'id': 'uncollected', 'label': '未加入集合', 'count': collections['uncollected']}],
                    keywords=[{'id': tag, 'label': tag, 'count': count} for tag, count in sorted(keywords.items())])

    def tree_response(self, snapshot, result, ids, mode, group_id, focus, limit, offset, include_context):
        tree = snapshot['tree']
        nodes = {n.id: n for n in tree.nodes}
        adjacency = defaultdict(set)
        predecessor = {}
        for edge in tree.edges:
            if edge.primary:
                adjacency[edge.source].add(edge.target)
                adjacency[edge.target].add(edge.source)
                predecessor[edge.target] = edge.source
        components = {}
        remaining = set(adjacency)
        while remaining:
            seed = min(remaining)
            component, queue = set(), [seed]
            while queue:
                current = queue.pop()
                if current not in component:
                    component.add(current)
                    queue.extend(adjacency[current] - component)
            remaining -= component
            components['branch:' + revision_of(sorted(component))] = component
        allowed = {nid for nid, n in nodes.items() if n.meta['paper_id'] in ids}
        result['facets'] = self.facets(snapshot)
        result['stats'].update(included=len(allowed), excluded=len(ids) - len(allowed))
        result['notes'] += tree.notes
        if mode == 'overview':
            result['groups'] = []
            for key, members in sorted(components.items()):
                selected = members & allowed
                if selected:
                    pids = sorted(nodes[n].meta['paper_id'] for n in selected)
                    anchor = nodes[min((n for n in members if n not in predecessor), default=min(members))].meta['paper_id']
                    branch = snapshot['nodes'][anchor].meta['branch']
                    motivation = snapshot['motivations'].get(anchor, {})
                    theme = next((motivation.get(field).strip() for field in ('theme', 'short_theme', 'direction')
                                  if isinstance(motivation.get(field), str) and 0 < len(motivation[field].strip()) <= 30), '')
                    label = snapshot.get('branch_names', {}).get(key) or theme or branch['label'] + ' · ' + snapshot['nodes'][anchor].label
                    result['groups'].append(self.card(snapshot, key, label, pids, 'branch', branch['label']))
            result.update(total=len(ids), shown=len(allowed), omitted=len(ids) - len(allowed))
            return result
        members = components.get(group_id, set()) if group_id else set(nodes)
        if members:
            roots_for_name = [n for n in members if predecessor.get(n) not in members]
            anchor = nodes[min(roots_for_name or members)].meta['paper_id']
            category = snapshot['nodes'][anchor].meta['branch']
            motivation = snapshot['motivations'].get(anchor, {})
            theme = next((motivation.get(field).strip() for field in ('theme', 'short_theme', 'direction')
                          if isinstance(motivation.get(field), str) and 0 < len(motivation[field].strip()) <= 30), '')
            result['scope_label'] = snapshot.get('branch_names', {}).get(group_id) or theme or category['label'] + ' · ' + snapshot['nodes'][anchor].label
        selected = members & allowed
        roots = sorted(n for n in selected if predecessor.get(n) not in selected)
        ordered, queue = [], deque(roots)
        while queue:
            node = queue.popleft()
            if node in ordered:
                continue
            ordered.append(node)
            queue.extend(sorted(n for n in adjacency[node] & selected if predecessor.get(n) == node))
        target = next((n for n in selected if nodes[n].meta['paper_id'] == focus), '')
        # Follow actual predecessors around a focus/search hit. Breadth-first
        # slicing can place unrelated siblings between the target and its parent.
        target = target or (ordered[0] if result['filters']['q'] and ordered else '')
        if target and not offset:
            chain = [target]
            structural = {**result['filters'], 'q': ''}
            structural_ids = set(self.select(snapshot, structural))
            while len(chain) < limit:
                parent = predecessor.get(chain[0])
                if parent not in members or parent in chain:
                    break
                # Search itself must not hide the path to a matching paper;
                # explicit category/collection/keyword filters remain boundaries.
                if nodes[parent].meta['paper_id'] not in structural_ids:
                    break
                chain.insert(0, parent)
            chosen = chain
        else:
            chosen = ordered[offset:offset + limit]
        boundary = [{'paper_id': nodes[n].meta['paper_id'], 'predecessor_id': nodes[predecessor[n]].meta['paper_id'],
                     'label': nodes[predecessor[n]].label,
                     'reason': 'filtered' if predecessor[n] not in allowed else 'pagination'}
                    for n in chosen if n in predecessor and predecessor[n] not in chosen]
        context = []
        if include_context:
            for current in chosen:
                parent = predecessor.get(current)
                if parent and parent not in chosen:
                    context = [parent]
                    break
        visible = set(chosen + context)
        graph = Graph(scope='motivation_tree', revision=snapshot['revision'])
        graph.focus = snapshot['nodes'][focus].id if focus in snapshot['nodes'] else ''
        graph.nodes = [deepcopy(nodes[n]) for n in chosen + context]
        for node in graph.nodes:
            pid = node.meta['paper_id']
            node.meta.update(deepcopy(snapshot['nodes'][pid].meta))
            node.meta['out_of_scope'] = node.id not in allowed
            node.meta['context'] = node.id in context or node.id not in allowed
            node.focus = pid == focus
        graph.edges = [e for e in tree.edges if e.primary and e.source in visible and e.target in visible]
        scoped_shown = len(set(chosen) & allowed)
        result.update(graph=graph.to_dict(), boundary=boundary, total=len(selected), shown=scoped_shown,
                      context_shown=len(graph.nodes) - scoped_shown,
                      omitted=max(0, len(selected) - scoped_shown), next_offset=offset + len(chosen),
                      collapsed=[{'after_id': nodes[chosen[-1]].meta['paper_id'] if chosen else '',
                                  'count': max(0, len(ordered) - offset - len(chosen)), 'label': '展开更多'}] if offset + len(chosen) < len(ordered) else [])
        return result

    def node_detail(self, paper_id):
        snapshot, rebuilding = self.ensure('graph')
        if not snapshot:
            return self.explore(mode='detail')
        if paper_id not in snapshot['nodes']:
            return None
        result = self.explore(mode='detail', focus=paper_id)
        result.pop('graph', None)
        relations = {'cites': [], 'cited_by': [], 'similar': []}
        for edge in snapshot['adjacency'].get(paper_id, []):
            side = 'similar' if edge['role'] == 'similar' else ('cites' if edge['source'] == paper_id else 'cited_by')
            other = edge['target'] if edge['source'] == paper_id else edge['source']
            relations[side].append({'paper_id': other, 'label': snapshot['nodes'][other].label, 'evidence': deepcopy(edge['evidence'])})
        node = snapshot['nodes'][paper_id]
        tree_snapshot, tree_pending = self.ensure('tree')
        tree = tree_snapshot['tree'] if tree_snapshot else Graph(scope='motivation_tree')
        result['motivation_pending'] = tree_pending
        tree_nodes = {item.id: item for item in tree.nodes}
        result['motivation_edges'] = [
            dict(edge.to_dict(), source_paper_id=tree_nodes[edge.source].meta['paper_id'],
                 target_paper_id=tree_nodes[edge.target].meta['paper_id'],
                 source_label=tree_nodes[edge.source].label, target_label=tree_nodes[edge.target].label)
            for edge in tree.edges
            if paper_id in (tree_nodes[edge.source].meta['paper_id'], tree_nodes[edge.target].meta['paper_id'])]
        result.update(node=deepcopy(node.to_dict()), paper_id=paper_id, relations=relations,
                      counts={key: len(rows) for key, rows in relations.items()}, collections=node.meta['collections'],
                      keywords=node.meta['tags'], motivation=deepcopy(snapshot['motivations'][paper_id]),
                      total=1, shown=1, omitted=0)
        return result
