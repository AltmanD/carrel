"""Read source titles without parsing a PDF or calling a model."""
from __future__ import annotations

from html.parser import HTMLParser

import httpx

from carrel.ids import ResolvedInput


class _CitationTitle(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ''

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'meta' and (values.get('name') or '').lower() == 'citation_title':
            self.title = values.get('content') or ''


def _clean_title(value) -> str:
    return ' '.join(value.split())[:1000] if isinstance(value, str) else ''


async def fetch_title(item: ResolvedInput, client: httpx.AsyncClient) -> str:
    aid = item.arxiv_id
    sources = ['hf', 'arxiv'] if item.kind == 'hf' else ['arxiv', 'hf']
    for source in sources:
        try:
            if source == 'hf':
                response = await client.get(f'https://huggingface.co/api/papers/{aid}')
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get('id') != aid:
                    continue
                title = _clean_title(payload.get('title'))
            else:
                response = await client.get(f'https://arxiv.org/abs/{aid}')
                response.raise_for_status()
                parser = _CitationTitle()
                parser.feed(response.text)
                title = _clean_title(parser.title)
            if title:
                return title
        except (httpx.HTTPError, ValueError):
            continue
    return ''
