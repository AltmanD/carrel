"""Graph eligibility and minimal, explicit prerequisite filling."""
from carrel.coverage import paper_coverage
from carrel.profile import Profile, stored_profile, estimate, task_estimates
from carrel.store.paths import paper_dir
from carrel.titles import project_title

GRAPH_REQUIRES = {
    'library': {'classified'},
    'paper': {'classified'},
    'motivation_tree': {'extract_key', 'summary', 'motivation', 'classified'},
}
CAPABILITY_LABELS = {'parsed': 'PDF 解析', 'extract_key': '关键章节笔记', 'summary': '总结',
                     'classified': '分类', 'motivation': '研究动机', 'extract_core': '核心章节笔记',
                     'extract_all': '全部章节笔记', 'translated': '译文', 'assessed': '阅读评估', 'relations': '引用与相似论文'}


def readiness(db, data_dir, graph, *, costs=True):
    if graph not in GRAPH_REQUIRES:
        raise ValueError('unknown graph')
    eligible, missing = [], []
    tasks = task_estimates(db) if costs else None
    for paper in db.list_papers():
        coverage = paper_coverage(paper_dir(data_dir, paper['id']))
        lacks = GRAPH_REQUIRES[graph] - coverage
        if not lacks:
            eligible.append(paper['id'])
            continue
        current = stored_profile(paper)
        minimum = Profile('key', False, 'classified' in lacks, False, 'motivation' in lacks, False)
        fill = current.union(minimum)
        cost = estimate(db, fill, coverage=coverage, current=current, tasks=tasks) if costs else {}
        missing.append({'paper_id': paper['id'], 'title': project_title(paper, paper_dir(data_dir, paper['id']))['display_title'],
                        'lacks': sorted(lacks), 'reasons': [CAPABILITY_LABELS[k] for k in sorted(lacks)],
                        'fill_profile': fill.to_dict(), 'est_tokens': cost.get('tokens', 0), 'est_usd': cost.get('usd', 0)})
    return {'graph': graph, 'eligible': eligible, 'missing': missing}
