import os
import re

import arxiv
import scrapy


class ArxivSpider(scrapy.Spider):
    name = "arxiv"  # 爬虫名称
    allowed_domains = ["arxiv.org"]  # 允许爬取的域名

    # One API request per this many papers: the arxiv client paginates id_list
    # lookups by its page_size, so 1000 papers cost ~10 requests instead of
    # ~1000 (each politely delayed by the client's built-in 3s spacing).
    METADATA_PAGE_SIZE = 100

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        categories = os.environ.get("CATEGORIES", "cs.CV")
        categories = categories.split(",")
        # 保存目标分类列表，用于后续验证
        self.target_categories = set(map(str.strip, categories))
        self.start_urls = [
            f"https://arxiv.org/list/{cat}/new" for cat in self.target_categories
        ]  # 起始URL（计算机科学领域的最新论文）
        # A cross-listed paper appears once per category listing; fetching its
        # metadata once is enough (enhance.py dedups by id again on load).
        self._seen_ids = set()
        self._client = arxiv.Client(page_size=self.METADATA_PAGE_SIZE)

    @staticmethod
    def _replacement_cutoff(response) -> "int | None":
        """Item number at which the Replacement section starts, if it exists.

        The old logic skipped everything after the LAST quick-link anchor,
        assuming it always marks the Replacement section. On a day with no
        Replacement section that skipped the Cross-lists instead, silently
        dropping every cross-listed paper. Identify the section by its link
        text instead of by position.
        """
        cutoffs = []
        for li in response.css("div[id=dlpage] ul li"):
            href = li.css("a::attr(href)").get()
            text = " ".join(li.css("a::text").getall())
            if href and "item" in href and "replacement" in text.lower():
                cutoffs.append(int(href.split("item")[-1]))
        return min(cutoffs) if cutoffs else None

    def _listing_ids(self, response) -> list:
        """Extract qualifying arXiv ids from one listing page (no network)."""
        cutoff = self._replacement_cutoff(response)
        ids = []
        for paper in response.css("dl dt"):
            paper_anchor = paper.css("a[name^='item']::attr(name)").get()
            if not paper_anchor:
                continue

            paper_id = int(paper_anchor.split("item")[-1])
            # Replacement 区块是旧论文的更新版，不属于"今日新论文"
            if cutoff is not None and paper_id >= cutoff:
                continue

            # 获取论文ID
            abstract_link = paper.css("a[title='Abstract']::attr(href)").get()
            if not abstract_link:
                continue

            arxiv_id = abstract_link.split("/")[-1]
            if arxiv_id in self._seen_ids:
                continue

            # 获取对应的论文描述部分 (dd元素)
            paper_dd = paper.xpath("following-sibling::dd[1]")
            if not paper_dd:
                continue

            # 提取论文分类信息 - 在subjects部分
            subjects_text = paper_dd.css(".list-subjects .primary-subject::text").get()
            if not subjects_text:
                # 如果找不到主分类，尝试其他方式获取分类
                subjects_text = paper_dd.css(".list-subjects::text").get()

            if subjects_text:
                # 解析分类信息，通常格式如 "Computer Vision and Pattern Recognition (cs.CV)"
                # 提取括号中的分类代码
                categories_in_paper = re.findall(r'\(([^)]+)\)', subjects_text)

                # 检查论文分类是否与目标分类有交集
                paper_categories = set(categories_in_paper)
                if not paper_categories.intersection(self.target_categories):
                    self.logger.debug(f"Skipped paper {arxiv_id} with categories {paper_categories} (not in target {self.target_categories})")
                    continue
            else:
                # 如果无法获取分类信息，记录警告但仍然返回论文（保持向后兼容）
                self.logger.warning(f"Could not extract categories for paper {arxiv_id}, including anyway")

            self._seen_ids.add(arxiv_id)
            ids.append(arxiv_id)
        return ids

    def parse(self, response):
        ids = self._listing_ids(response)
        self.logger.info(f"{response.url}: {len(ids)} new paper(s) to fetch")
        yield from self._fetch_metadata(ids)

    def _fetch_metadata(self, ids):
        """Fetch full metadata in batches and yield one record per paper.

        Batched id_list lookup: one request covers METADATA_PAGE_SIZE papers
        instead of one request per paper (the old pipeline behaviour, which
        made a 1200-paper day take ~1 hour of 3s-spaced requests).
        """
        for start in range(0, len(ids), self.METADATA_PAGE_SIZE):
            chunk = ids[start:start + self.METADATA_PAGE_SIZE]
            try:
                results = list(self._client.results(arxiv.Search(id_list=chunk)))
            except Exception as error:
                # The client already retried internally. Dropping a whole chunk
                # silently would lose up to 100 papers, so log loudly.
                self.logger.error(
                    f"Metadata fetch failed for {len(chunk)} ids "
                    f"({chunk[0]}..{chunk[-1]}): {error}"
                )
                continue

            returned = set()
            for paper in results:
                # e.g. "2610.19007v1" -> "2610.19007"
                arxiv_id = paper.get_short_id().rsplit("v", 1)[0]
                returned.add(arxiv_id)
                yield {
                    "id": arxiv_id,
                    "categories": paper.categories,
                    "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
                    "abs": f"https://arxiv.org/abs/{arxiv_id}",
                    "authors": [a.name for a in paper.authors],
                    "title": paper.title,
                    "comment": paper.comment,
                    "summary": paper.summary,
                }

            missing = sorted(set(chunk) - returned)
            if missing:
                self.logger.warning(
                    f"{len(missing)} id(s) returned no metadata (withdrawn?): {missing}"
                )
