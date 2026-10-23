"""RF11 text-search recipes with an input-derived ranking oracle.

Each document has two tokens. The oracle computes BM25 from those input tokens,
including the ID tie breaker, without asking the database for expected rows.
"""
from __future__ import annotations

import math
import time


FAMILY = "fts_matrix"


def validate(scenario):
    if scenario.fts_statistics_mode not in ("wal", "durable"):
        raise ValueError("invalid FTS statistics mode")
    if scenario.fts_history not in (0, 1, 8, 32):
        raise ValueError("unsupported FTS history slice")
    if scenario.fts_statistics_mode == "wal" and scenario.fts_history:
        raise ValueError("WAL statistics cannot retain durable history")
    if scenario.fts_updates not in (0, 1, 4):
        raise ValueError("unsupported FTS update slice")
    if scenario.fts_write_case not in ("none", "insert", "update", "delete", "external_mixed"):
        raise ValueError("unsupported FTS write slice")
    if scenario.fts_write_case != "none" and (scenario.fts_updates or scenario.reader != "short"
                                              or scenario.nodes != 256 or scenario.cache != "reopen"
                                              or (scenario.fts_statistics_mode, scenario.fts_history)
                                              not in (("wal", 0), ("durable", 8))):
        raise ValueError("FTS write costs require the paired 256-document reopen slice")
    if scenario.reader == "retained" and (scenario.nodes != 256 or scenario.fts_updates != 4):
        raise ValueError("retained reader requires the four-update 256-document slice")
    if scenario.reader == "short" and scenario.fts_updates and not (
            scenario.fts_updates == 1 and scenario.fts_statistics_mode == "wal"
            and scenario.cache == "warm"):
        raise ValueError("short-reader update is only the warm WAL-delta slice")
    if scenario.nodes not in (256, 4096, 65536) or scenario.term not in ("common", "rare"):
        raise ValueError("unsupported FTS corpus or term")
    if scenario.nodes == 65536 and scenario.term != "rare":
        raise ValueError("large corpus uses bounded rare-term result")
    if scenario.query_memory_budget_bytes is not None:
        raise ValueError("native FTS is outside the Cypher query memory budget observer")


def matrix(Scenario):
    rows = []
    for mode, history in (("wal", 0), ("durable", 0), ("durable", 1),
                          ("durable", 8), ("durable", 32)):
        for term in ("rare", "common"):
            for cache in ("warm", "reopen"):
                rows.append(Scenario(f"fts-matrix-n256-{mode}-h{history}-{term}-{cache}", FAMILY,
                                     nodes=256, edges_per_node=0, fts_statistics_mode=mode,
                                     fts_history=history, term=term, cache=cache))
    for nodes, mode, history in ((4096, "wal", 0), (4096, "durable", 8),
                                 (65536, "wal", 0), (65536, "durable", 32)):
        rows.append(Scenario(f"fts-matrix-n{nodes}-{mode}-h{history}-rare-reopen", FAMILY,
                             nodes=nodes, edges_per_node=0, fts_statistics_mode=mode,
                             fts_history=history, term="rare", cache="reopen"))
    for history in (0, 1, 8, 32):
        rows.append(Scenario(f"fts-matrix-retained-h{history}", FAMILY, nodes=256,
                             edges_per_node=0, fts_statistics_mode="durable",
                             fts_history=history, term="common", cache="reopen",
                             reader="retained", fts_updates=4))
    rows.append(Scenario("fts-matrix-wal-delta-n256", FAMILY, nodes=256,
                         edges_per_node=0, fts_statistics_mode="wal", fts_history=0,
                         term="common", cache="warm", fts_updates=1))
    for mode, history in (("wal", 0), ("durable", 8)):
        for write_case in ("insert", "update", "delete", "external_mixed"):
            rows.append(Scenario(f"fts-matrix-{mode}-{write_case}-n256", FAMILY,
                                 nodes=256, edges_per_node=0,
                                 fts_statistics_mode=mode, fts_history=history,
                                 term="common", cache="reopen", fts_write_case=write_case))
    return rows


def contract(scenario):
    return {"fts_projection": "ordered (record_id, exact BM25 score) with stable ID ties",
            "statistics_mode": scenario.fts_statistics_mode,
            "statistics_history_entries": scenario.fts_history,
            "retained_reader_updates": scenario.fts_updates,
            "write_case": scenario.fts_write_case,
            "expected_statistics_source": expected_source(scenario),
            "corpus_documents": scenario.nodes,
            "memory_limit": "native search has no Cypher logical-memory peak observer; process RSS only"}


def oracle(scenario):
    corpus = {i + 1: (["common", "rare"] if i == 0 else ["common", "usual"])
              for i in range(scenario.nodes)}
    if scenario.fts_write_case == "insert":
        corpus[scenario.nodes + 1] = ["common", "usual"]
    elif scenario.fts_write_case == "update":
        corpus[2] = ["common", "usual", "extra"]
    elif scenario.fts_write_case == "delete":
        del corpus[3]
    elif scenario.fts_write_case == "external_mixed":
        corpus[2] = ["common", "common", "usual", "extra"]
        del corpus[3]
        corpus[scenario.nodes + 1] = ["common", "extra"]
        del corpus[4]
    elif scenario.fts_updates == 1:
        corpus[2] = ["common", "usual", "extra"]
    count = len(corpus)
    frequency = sum(scenario.term in tokens for tokens in corpus.values())
    inverse_frequency = math.log1p((count - frequency + .5) / (frequency + .5))
    mean_length = sum(map(len, corpus.values())) / count
    ranked = []
    for record_id, tokens in corpus.items():
        tf = tokens.count(scenario.term)
        if not tf:
            continue
        score = (inverse_frequency * tf * (1.2 + 1)
                 / (tf + 1.2 * (1 - .75 + .75 * len(tokens) / mean_length)))
        ranked.append((record_id, score))
    return tuple(sorted(ranked, key=lambda row: (-row[1], row[0])))


def corpus_documents(scenario):
    return scenario.nodes + (scenario.fts_write_case == "insert") - (
        scenario.fts_write_case in ("delete", "external_mixed"))


def query_for(scenario):
    return "native search_text ranked projection", {
        "term": scenario.term, "k": min(corpus_documents(scenario), 10000),
        "statistics_mode": scenario.fts_statistics_mode,
        "statistics_history_entries": scenario.fts_history}


def build_fixture(db, scenario):
    from okto_grafx import TextIndexOptions

    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE N(id INT64,body STRING,PRIMARY KEY(id))")
    for start in range(0, scenario.nodes, 512):
        with db.begin("write") as tx:
            tx.execute("UNWIND $rows AS r CREATE (:N {id:r.id,body:r.body})", {
                "rows": [{"id": i, "body": "common rare" if i == 0 else "common usual"}
                         for i in range(start, min(start + 512, scenario.nodes))]})
    db.create_text_index("campaign_text", "N", ("body",), options=TextIndexOptions(
        statistics_mode=scenario.fts_statistics_mode,
        statistics_history_entries=scenario.fts_history))
    db.checkpoint()


def apply_updates(db, scenario):
    commit_times = []
    for i in range(1, scenario.fts_updates + 1):
        tx = db.begin("write")
        try:
            tx.execute("MATCH (n:N {id:$id}) SET n.body=$body",
                       {"id": i, "body": "common usual extra"})
            start = time.perf_counter_ns()
            ack = tx.commit()
            if not ack.durable or not ack.wrote:
                raise ValueError("FTS update lacks a durable write ACK")
            commit_times.append(time.perf_counter_ns() - start)
        except BaseException:
            tx.rollback()
            raise
    return tuple(commit_times)


def apply_write_case(db, scenario, path):
    """Measure distinct durable units; the external case uses a second handle."""
    from okto_grafx import connect

    statements = {
        "insert": ("CREATE (:N {id:256,body:'common usual'})",),
        "update": ("MATCH (n:N {id:1}) SET n.body='common usual extra'",),
        "delete": ("MATCH (n:N {id:2}) DELETE n",),
        "external_mixed": (
            "MATCH (n:N {id:1}) SET n.body='common common usual extra'",
            "MATCH (n:N {id:2}) DELETE n",
            "CREATE (:N {id:256,body:'common extra'})",
            "MATCH (n:N {id:3}) DELETE n",
        ),
    }[scenario.fts_write_case]
    durations = []
    if scenario.fts_write_case == "external_mixed":
        writer = connect(path)
    else:
        writer = db
    try:
        for statement in statements:
            tx = writer.begin("write")
            try:
                tx.execute(statement)
                start = time.perf_counter_ns()
                ack = tx.commit()
                durations.append(time.perf_counter_ns() - start)
                if not ack.durable or not ack.wrote:
                    raise ValueError("FTS maintenance lacks a durable write ACK")
            except BaseException:
                if tx.active:
                    tx.rollback()
                raise
    finally:
        if writer is not db:
            writer.close()
    return tuple(durations)


def expected_source(scenario):
    if scenario.fts_updates == 1:
        return "wal_delta"
    if scenario.reader == "retained":
        return "durable_summary" if scenario.fts_history in (8, 32) else "full_census"
    if scenario.cache == "warm":
        return "snapshot_cache"
    return "full_census" if scenario.fts_statistics_mode == "wal" else "durable_summary"


def rows(db, scenario, *, reader=None):
    result = db.search_text(reader, index="campaign_text", query=scenario.term,
                            k=min(corpus_documents(scenario), 10000)) if reader is not None else db.search_text(
                                index="campaign_text", query=scenario.term,
                                k=min(corpus_documents(scenario), 10000))
    return tuple((hit.record_id, hit.score) for hit in result.hits), result


def statistics(result):
    regime = result.statistics_regime
    if not regime or not regime.replace("_", "").isalnum():
        raise ValueError("unrecognized FTS statistics provenance")
    return {"postings_visited": result.postings_visited,
            "candidates": result.candidates,
            "corpus_documents": result.corpus_documents,
            "statistics_wal_records": result.statistics_wal_records,
            f"statistics_from_{regime}": 1}
