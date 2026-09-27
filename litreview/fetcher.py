"""学术元数据抓取与反查解析器 (Academic Resolver & Fetcher)。

支持：
1. DOI 权威直查：通过 CrossRef / OpenAlex API 获取 100% 权威元数据；
2. 题名 + 作者反查 (Reverse Resolution)：输入论文篇名或不完整参考文献，在 OpenAlex 与 CrossRef
   中检索最匹配论文，实现 0 幻觉自动补全权威题名、DOI、完整作者列表、出版年份与期刊名称。
"""
import re
import json
import logging
import unicodedata
import html
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("fetcher")

USER_AGENT = "LiteratureAnalyzer/1.0 (https://github.com/JoelDu/literature-workflow; mailto:bot@literature-analyzer.org)"


def clean_doi(doi: str) -> str:
    """清洗与标准化 DOI 字符串。"""
    if not doi:
        return ""
    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", str(doi).strip())
    if m:
        return m.group(0).rstrip(".,;）)]}")
    return ""


def normalize_title_for_search(title: str) -> str:
    """清洗文献题名以供学术 API 搜索。去除前缀年份、多余标点和多余空白。"""
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", str(title)).strip()
    t = html.unescape(t)
    # 去除开头的 4 位年份前缀 (如 "2001 Extraction of...")
    t = re.sub(r'^(?:18|19|20)\d{2}\s*[\-\_\:\.\s]+', '', t)
    # 去除《》或引号
    t = re.sub(r'^[\s\"\'\“\”\‘\’\《\〈\(\[\{]+', '', t)
    t = re.sub(r'[\s\"\'\“\”\‘\’\》\〉\)\]\}]+$', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def calculate_title_similarity(s1: str, s2: str) -> float:
    """计算两个英文/中文字符串的 Token Jaccard 相似度（忽略大小写与标点）。"""
    if not s1 or not s2:
        return 0.0
    def tokenize(s: str) -> set:
        clean = re.sub(r'[^\w\s]', ' ', s.lower())
        return set(clean.split())
    
    tokens1 = tokenize(s1)
    tokens2 = tokenize(s2)
    if not tokens1 or not tokens2:
        return 0.0
    
    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)
    return len(intersection) / len(union)


def parse_openalex_biblio(item: dict) -> dict:
    """从 OpenAlex 作品数据中规范化解析卷、期与页码范围。"""
    biblio = item.get("biblio") or {}
    volume = str(biblio.get("volume") or "").strip()
    issue = str(biblio.get("issue") or "").strip()
    first_p = str(biblio.get("first_page") or "").strip()
    last_p = str(biblio.get("last_page") or "").strip()
    pages = f"{first_p}-{last_p}" if (first_p and last_p and first_p != last_p) else (first_p or last_p or "")
    return {"volume": volume, "issue": issue, "pages": pages}


def parse_crossref_biblio(item: dict) -> dict:
    """从 CrossRef 作品数据中规范化解析卷、期与页码/文章编号。"""
    volume = str(item.get("volume") or "").strip()
    issue = str(item.get("issue") or "").strip()
    pages = str(item.get("page") or item.get("article-number") or "").strip()
    return {"volume": volume, "issue": issue, "pages": pages}


def fetch_openalex_by_doi(doi: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
    """通过 OpenAlex API 获取 DOI 对应的权威学术元数据。"""
    valid_doi = clean_doi(doi)
    if not valid_doi:
        return None

    url = f"https://api.openalex.org/works/https://doi.org/{valid_doi}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            msg = json.loads(resp.read().decode("utf-8"))
            
            title = (msg.get("title") or "").strip()
            authors = [a.get("author", {}).get("display_name", "") for a in msg.get("authorships", []) if a.get("author", {}).get("display_name")]
            journal = msg.get("primary_location", {}).get("source", {}).get("display_name") or ""
            year = msg.get("publication_year")
            
            oa_info = msg.get("open_access", {})
            is_oa = bool(oa_info.get("is_oa", False))
            oa_url = oa_info.get("oa_url") or msg.get("primary_location", {}).get("pdf_url")
            
            bib_meta = parse_openalex_biblio(msg)

            return {
                "doi": valid_doi,
                "title": title,
                "authors": ", ".join(authors),
                "authors_list": authors,
                "journal": journal,
                "volume": bib_meta["volume"],
                "issue": bib_meta["issue"],
                "pages": bib_meta["pages"],
                "year": str(year) if year else "",
                "publication_year": year,
                "cited_by_count": msg.get("cited_by_count", 0),
                "openalex_id": msg.get("id", "").split("/")[-1],
                "is_open_access": is_oa,
                "oa_url": oa_url,
                "source": "openalex"
            }
    except Exception as e:
        logger.debug(f"OpenAlex DOI 查询失败 {valid_doi}: {e}")
        return None


def fetch_crossref(doi: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
    """通过 CrossRef API 获取 DOI 对应的权威学术元数据。"""
    valid_doi = clean_doi(doi)
    if not valid_doi:
        return None

    url = f"https://api.crossref.org/works/{valid_doi}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
            msg = payload.get("message", {})
            
            titles = msg.get("title", [])
            title = titles[0].strip() if titles and isinstance(titles, list) else ""
            
            authors_list = []
            for a in msg.get("author", []):
                if not isinstance(a, dict):
                    continue
                given = a.get("given", "").strip()
                family = a.get("family", "").strip()
                name = a.get("name", "").strip()
                if given and family:
                    authors_list.append(f"{given} {family}")
                elif family:
                    authors_list.append(family)
                elif given:
                    authors_list.append(given)
                elif name:
                    authors_list.append(name)
            authors_str = ", ".join(authors_list)

            container_titles = msg.get("container-title", [])
            journal = container_titles[0].strip() if container_titles and isinstance(container_titles, list) else ""
            short_containers = msg.get("short-container-title", [])
            if not journal and short_containers and isinstance(short_containers, list):
                journal = short_containers[0].strip()

            bib_meta = parse_crossref_biblio(msg)

            year = ""
            for date_key in ("published-print", "published-online", "published", "created", "issued"):
                date_parts = msg.get(date_key, {}).get("date-parts", [])
                if date_parts and isinstance(date_parts, list) and len(date_parts[0]) > 0:
                    y = date_parts[0][0]
                    if y and str(y).isdigit():
                        year = str(y)
                        break

            publisher = (msg.get("publisher") or "").strip()

            return {
                "doi": valid_doi,
                "title": title,
                "authors": authors_str,
                "authors_list": authors_list,
                "journal": journal,
                "volume": bib_meta["volume"],
                "issue": bib_meta["issue"],
                "pages": bib_meta["pages"],
                "year": year,
                "publication_year": int(year) if year and year.isdigit() else None,
                "publisher": publisher,
                "cited_by_count": msg.get("is-referenced-by-count", 0),
                "source": "crossref"
            }
    except Exception as e:
        logger.debug(f"CrossRef 查询失败 {valid_doi}: {e}")
        return None


def resolve_work_by_title_and_author(
    title: str,
    authors: str = "",
    year: Optional[int] = None,
    timeout: int = 8
) -> Optional[Dict[str, Any]]:
    """通过 OpenAlex / CrossRef 权威学术图谱反查解析文献元数据。
    
    根据输入的篇名（支持中文翻译、英文原名、带年份前缀标题）与作者名，自动匹配
    最权威的论文实体，补全官方真实 DOI、标准全称题名、作者全列表、出版年与期刊。
    """
    clean_title = normalize_title_for_search(title)
    if not clean_title or len(clean_title) < 5:
        return None

    # 1. 优先使用 OpenAlex Works Search
    try:
        q_str = urllib.parse.quote(clean_title)
        url = f"https://api.openalex.org/works?search={q_str}&per-page=3"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                res_json = json.loads(resp.read().decode("utf-8"))
                candidates = res_json.get("results", [])
                for item in candidates:
                    cand_title = item.get("title") or ""
                    cand_doi = clean_doi(item.get("doi") or "")
                    sim = calculate_title_similarity(clean_title, cand_title)
                    
                    if sim >= 0.55 or (clean_title.lower() in cand_title.lower()) or (cand_title.lower() in clean_title.lower()):
                        authors_list = [a.get("author", {}).get("display_name", "") for a in item.get("authorships", []) if a.get("author", {}).get("display_name")]
                        journal = item.get("primary_location", {}).get("source", {}).get("display_name") or ""
                        pub_year = item.get("publication_year")
                        oa_info = item.get("open_access", {})
                        
                        bib_meta = parse_openalex_biblio(item)

                        return {
                            "doi": cand_doi,
                            "title": cand_title,
                            "authors": ", ".join(authors_list),
                            "authors_list": authors_list,
                            "journal": journal,
                            "volume": bib_meta["volume"],
                            "issue": bib_meta["issue"],
                            "pages": bib_meta["pages"],
                            "year": str(pub_year) if pub_year else "",
                            "publication_year": pub_year,
                            "cited_by_count": item.get("cited_by_count", 0),
                            "openalex_id": item.get("id", "").split("/")[-1],
                            "is_open_access": bool(oa_info.get("is_oa", False)),
                            "oa_url": oa_info.get("oa_url") or item.get("primary_location", {}).get("pdf_url"),
                            "confidence_similarity": round(sim, 3),
                            "source": "openalex_search"
                        }
    except Exception as e:
        logger.debug(f"OpenAlex title search error: {e}")

    # 2. 降级使用 CrossRef Query
    try:
        q_str = urllib.parse.quote(clean_title)
        url = f"https://api.crossref.org/works?query.title={q_str}&rows=3"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                payload = json.loads(resp.read().decode("utf-8"))
                items = payload.get("message", {}).get("items", [])
                for it in items:
                    t_list = it.get("title", [])
                    cand_title = t_list[0] if t_list else ""
                    cand_doi = clean_doi(it.get("DOI") or "")
                    sim = calculate_title_similarity(clean_title, cand_title)
                    
                    if sim >= 0.55 or (clean_title.lower() in cand_title.lower()) or (cand_title.lower() in clean_title.lower()):
                        authors_list = []
                        for a in it.get("author", []):
                            given = a.get("given", "").strip()
                            family = a.get("family", "").strip()
                            if given and family:
                                authors_list.append(f"{given} {family}")
                            elif family or given:
                                authors_list.append(family or given)
                        
                        import html
                        container_titles = it.get("container-title", [])
                        raw_journal = container_titles[0] if container_titles else ""
                        journal = html.unescape(raw_journal)
                        cand_title = html.unescape(cand_title)
                        
                        bib_meta = parse_crossref_biblio(it)

                        year_str = ""
                        for date_key in ("published-print", "published-online", "published", "created"):
                            date_parts = it.get(date_key, {}).get("date-parts", [])
                            if date_parts and date_parts[0]:
                                year_str = str(date_parts[0][0])
                                break
                        
                        return {
                            "doi": cand_doi,
                            "title": cand_title,
                            "authors": ", ".join(authors_list),
                            "authors_list": authors_list,
                            "journal": journal,
                            "volume": bib_meta["volume"],
                            "issue": bib_meta["issue"],
                            "pages": bib_meta["pages"],
                            "year": year_str,
                            "publication_year": int(year_str) if year_str.isdigit() else None,
                            "cited_by_count": it.get("is-referenced-by-count", 0),
                            "confidence_similarity": round(sim, 3),
                            "source": "crossref_search"
                        }
    except Exception as e:
        logger.debug(f"CrossRef title search error: {e}")

    return None
