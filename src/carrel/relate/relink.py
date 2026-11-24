"""Rebuild local citation edges deterministically without network requests."""
from __future__ import annotations


def relink_library(db):
    from carrel.ids import resolve_input, UnsupportedInput
    with db.transaction() as conn:
        papers = {row['id']: dict(row) for row in conn.execute('SELECT * FROM papers ORDER BY id')}
        known = {pid.split(':', 1)[1].lower(): pid for pid in papers if pid.startswith(('arxiv:', 'doi:'))}
        for pid, paper in papers.items():
            try:
                resolved = resolve_input(paper.get('source_url') or '')
                if resolved.arxiv_id:
                    known.setdefault(resolved.arxiv_id.lower(), pid)
            except UnsupportedInput:
                pass
        # Only derived rows are removed. Provider evidence is preserved.
        conn.execute("DELETE FROM relations WHERE provider IN ('local','local_reverse')")
        rows = [dict(row) for row in conn.execute('SELECT * FROM relations ORDER BY src_id, kind, rank, dst_id')]
        for row in rows:
            target = known.get((row.get('dst_arxiv') or '').lower()) or known.get((row.get('dst_doi') or '').lower())
            if target and target != row['src_id'] and target != row['dst_id']:
                # Multiple provider identifiers may resolve to the same paper.
                conn.execute('DELETE FROM relations WHERE src_id=? AND dst_id=? AND kind=?',
                             (row['src_id'], row['dst_id'], row['kind']))
                row['dst_id'] = target
                keys = list(row)
                conn.execute(f"INSERT OR IGNORE INTO relations({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                             [row[k] for k in keys])
        # A cites B means B is cited_by A. Reverse both provider directions.
        rows = [dict(row) for row in conn.execute("SELECT * FROM relations WHERE kind IN ('cites','cited_by') ORDER BY src_id,dst_id,kind")]
        for row in rows:
            src, dst = row['src_id'], row['dst_id']
            if src == dst or src not in papers or dst not in papers:
                continue
            reverse = dict(row, src_id=dst, dst_id=src, kind='cited_by' if row['kind'] == 'cites' else 'cites',
                           provider='local_reverse', dst_title=papers[src]['title'], dst_year=papers[src]['year'],
                           dst_arxiv=src.removeprefix('arxiv:') if src.startswith('arxiv:') else '',
                           dst_doi=src.removeprefix('doi:') if src.startswith('doi:') else '',
                           dst_url=papers[src]['source_url'], rank=0)
            keys = list(reverse)
            conn.execute(f"INSERT OR IGNORE INTO relations({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                         [reverse[k] for k in keys])
