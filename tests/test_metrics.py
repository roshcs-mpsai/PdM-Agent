"""Metrics (OPS-02, T24): pdm_common.metrics and the Prometheus/Grafana files in ops/.

The exposition is read back with a small parser of the text format 0.0.4, so the
tests check what Prometheus would read, not the client's internals. The
dashboards and alert rules are checked against the metric catalogue: a panel
that names a metric no service exports would show "No data" forever.
Prometheus and Grafana themselves are not run here.
"""
import json
import math
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from pdm_common import metrics
from pdm_common.metrics import (CATALOGUE, DEFAULT_BUCKETS, PORTS, SERVICE, STANDARD, Counter, Gauge, Histogram,
                                Registry, serve, service_metrics)

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops"
DASHBOARDS = sorted((OPS / "grafana" / "dashboards").glob("*.json"))
BUILTIN = {"up"}                      # series Prometheus itself creates per target


# --- a reader for the text format ---------------------------------------------------------------

SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"(,|$)')
UNESCAPE = {"n": "\n", "\\": "\\", '"': '"'}


def _unescape(text):
    return re.sub(r"\\(.)", lambda m: UNESCAPE[m.group(1)], text)


def parse(text):
    """{family: {"help", "type", "samples": [(name, {label: value}, float)]}}; asserts the
    layout rules on the way: HELP then TYPE before a family's samples, families contiguous,
    every line terminated."""
    assert text.endswith("\n")
    families, current = {}, None
    for line in text.splitlines():
        assert line, "no blank lines"
        if line.startswith("# HELP "):
            current, _, doc = line[7:].partition(" ")
            assert current not in families, f"{current} appears twice"
            families[current] = {"help": _unescape(doc), "type": None, "samples": []}
        elif line.startswith("# TYPE "):
            name, kind = line[7:].split(" ")
            assert name == current and families[name]["type"] is None and not families[name]["samples"]
            families[name]["type"] = kind
        else:
            m = SAMPLE.match(line)
            assert m, f"not a sample line: {line!r}"
            name, labels_text, number = m.groups()
            labels, pos = {}, 0
            while labels_text and pos < len(labels_text):
                lm = LABEL.match(labels_text, pos)
                assert lm, f"bad labels in {line!r}"
                labels[lm.group(1)] = _unescape(lm.group(2))
                pos = lm.end()
            fam = families[current]
            assert fam["type"] is not None, f"{name}: sample before its TYPE line"
            if fam["type"] == "histogram":
                assert name in (current + "_bucket", current + "_sum", current + "_count"), (name, current)
            else:
                assert name == current, (name, current)
            fam["samples"].append((name, labels, float(number)))
    return families


def value(families, family, sample=None, **labels):
    found = [v for n, ls, v in families[family]["samples"] if n == (sample or family) and ls == labels]
    assert len(found) == 1, (family, sample, labels, found)
    return found[0]


# --- exposition ---------------------------------------------------------------------------------

def test_counter_and_gauge_with_help_and_type():
    r = Registry()
    c = Counter("pdm_test_events_total", "Events seen", ["kind"], registry=r)
    c.labels(kind="a").inc()
    c.labels(kind="a").inc(2)
    c.labels("b").inc(0.5)
    g = Gauge("pdm_test_level", "A level", registry=r)
    g.set(4)
    g.dec(1.5)
    fam = parse(r.exposition())
    assert fam["pdm_test_events_total"]["type"] == "counter"
    assert fam["pdm_test_events_total"]["help"] == "Events seen"
    assert value(fam, "pdm_test_events_total", kind="a") == 3.0
    assert value(fam, "pdm_test_events_total", kind="b") == 0.5
    assert fam["pdm_test_level"]["type"] == "gauge"
    assert value(fam, "pdm_test_level") == 2.5
    # unlabelled metrics export 0 before first use; labelled ones only once a label set exists
    r2 = Registry()
    Counter("pdm_quiet_total", "Never incremented", registry=r2)
    Counter("pdm_labelled_total", "Never used", ["x"], registry=r2)
    fam2 = parse(r2.exposition())
    assert value(fam2, "pdm_quiet_total") == 0.0
    assert fam2["pdm_labelled_total"]["samples"] == []


def test_label_values_and_help_are_escaped():
    r = Registry()
    awkward = 'say "hi"\\back\nslash'
    c = Counter("pdm_odd_total", 'Help with a \\ backslash\nand a newline', ["v"], registry=r)
    c.labels(v=awkward).inc()
    text = r.exposition()
    assert '# HELP pdm_odd_total Help with a \\\\ backslash\\nand a newline' in text
    assert 'v="say \\"hi\\"\\\\back\\nslash"' in text
    fam = parse(text)
    assert fam["pdm_odd_total"]["help"] == 'Help with a \\ backslash\nand a newline'
    assert value(fam, "pdm_odd_total", v=awkward) == 1.0


def test_special_values_are_written_as_prometheus_reads_them():
    r = Registry()
    g = Gauge("pdm_special", "Special values", ["k"], registry=r)
    g.labels(k="inf").set(math.inf)
    g.labels(k="minus").set(-math.inf)
    g.labels(k="nan").set(math.nan)
    g.labels(k="big").set(12345678.0)
    text = r.exposition()
    assert 'pdm_special{k="inf"} +Inf' in text
    assert 'pdm_special{k="minus"} -Inf' in text
    assert 'pdm_special{k="nan"} NaN' in text
    assert 'pdm_special{k="big"} 1.2345678e+07' in text
    fam = parse(text)
    assert value(fam, "pdm_special", k="big") == 12345678.0


def test_histogram_buckets_are_cumulative_with_sum_and_count():
    r = Registry()
    h = Histogram("pdm_test_seconds", "Durations", ["topic"], buckets=(0.1, 1.0, 2.0), registry=r)
    child = h.labels(topic="t")
    observations = [0.05, 0.1, 0.5, 1.0, 1.5, 2.0, 3.0, 30.0]
    for v in observations:
        child.observe(v)
    fam = parse(r.exposition())
    assert fam["pdm_test_seconds"]["type"] == "histogram"
    buckets = [(ls["le"], v) for n, ls, v in fam["pdm_test_seconds"]["samples"] if n == "pdm_test_seconds_bucket"]
    # le is inclusive: 0.1 lands in the 0.1 bucket, 2.0 in the 2.0 bucket
    assert buckets == [("0.1", 2.0), ("1.0", 4.0), ("2.0", 6.0), ("+Inf", 8.0)]
    counts = [v for _, v in buckets]
    assert counts == sorted(counts)
    assert value(fam, "pdm_test_seconds", "pdm_test_seconds_count", topic="t") == len(observations)
    assert value(fam, "pdm_test_seconds", "pdm_test_seconds_sum", topic="t") == pytest.approx(sum(observations))
    # the le label comes last, after the metric's own labels
    assert 'pdm_test_seconds_bucket{topic="t",le="+Inf"} 8.0' in r.exposition()


def test_default_buckets_run_from_1_ms_to_10_s_with_an_edge_at_2_s():
    assert DEFAULT_BUCKETS[0] == 0.001 and DEFAULT_BUCKETS[-1] == 10.0
    assert 2.0 in DEFAULT_BUCKETS
    r = Registry()
    Histogram("pdm_default_seconds", "Default buckets", registry=r).observe(0.003)
    les = [ls["le"] for n, ls, _ in parse(r.exposition())["pdm_default_seconds"]["samples"] if n.endswith("_bucket")]
    assert les[0] == "0.001" and "2.0" in les and les[-2:] == ["10.0", "+Inf"]


def test_time_works_as_context_manager_and_decorator():
    r = Registry()
    h = Histogram("pdm_timed_seconds", "Timed", registry=r)
    with h.time() as t:
        pass
    assert t.elapsed is not None and t.elapsed >= 0

    @h.time()
    def work(x):
        return x * 2

    assert work(21) == 42
    with pytest.raises(RuntimeError):
        with h.time():
            raise RuntimeError("observed anyway")
    assert r.value("pdm_timed_seconds_count") == 3
    labelled = Histogram("pdm_timed_l_seconds", "Timed, labelled", ["topic"], registry=r)
    with labelled.labels(topic="x").time():
        pass
    assert r.value("pdm_timed_l_seconds", topic="x") == 1


def test_mistakes_are_refused():
    r = Registry()
    c = Counter("pdm_checked_total", "Checked", ["a", "b"], registry=r)
    with pytest.raises(ValueError, match="takes labels"):
        c.labels(a="1")
    with pytest.raises(ValueError, match="takes labels"):
        c.labels(a="1", b="2", c="3")
    with pytest.raises(ValueError, match="label value"):
        c.labels("1")
    with pytest.raises(ValueError, match="call .labels"):
        c.inc()
    with pytest.raises(ValueError, match="only goes up"):
        c.labels("1", "2").inc(-1)
    with pytest.raises(ValueError, match="end in _total"):
        Counter("pdm_bad", "No suffix", registry=None)
    with pytest.raises(ValueError, match="already registered"):
        Counter("pdm_checked_total", "Again", registry=r)
    with pytest.raises(ValueError, match="already registered as a counter"):
        r.gauge("pdm_checked_total", "Same name, other kind")
    with pytest.raises(ValueError, match="not a valid metric name"):
        Gauge("pdm-dash", "Dash", registry=None)
    with pytest.raises(ValueError, match="reserved"):
        Histogram("pdm_le_seconds", "le label", ["le"], registry=None)
    with pytest.raises(ValueError, match="no labels"):
        Gauge("pdm_plain", "Plain", registry=None).labels(x="1")
    # get-or-create: the same definition returns the same metric
    assert r.counter("pdm_checked_total", "Checked", ["a", "b"]) is c


def test_gauge_function_is_read_at_scrape_time_and_a_failure_gives_nan():
    r = Registry()
    g = Gauge("pdm_queue", "Queue", ["queue"], registry=r)
    items = [1, 2, 3]
    g.labels(queue="ok").set_function(lambda: len(items))
    g.labels(queue="broken").set_function(lambda: 1 / 0)
    items.append(4)
    fam = parse(r.exposition())
    assert value(fam, "pdm_queue", queue="ok") == 4.0
    assert math.isnan(value(fam, "pdm_queue", queue="broken"))
    inflight = Gauge("pdm_inflight", "In flight", registry=r)
    with inflight.track_inprogress():
        assert inflight.value == 1
    assert inflight.value == 0


def test_concurrent_increments_lose_nothing():
    r = Registry()
    c = Counter("pdm_race_total", "Race", ["topic"], registry=r)
    plain = Counter("pdm_race_plain_total", "Race, no labels", registry=r)
    g = Gauge("pdm_race_gauge", "Race gauge", registry=r)
    h = Histogram("pdm_race_seconds", "Race histogram", registry=r)
    threads_n, per_thread = 8, 5000
    start = threading.Barrier(threads_n + 2)       # the workers, the scraper and this thread
    errors = []

    def work(i):
        start.wait()
        for k in range(per_thread):
            c.labels(topic=f"t{k % 3}").inc()
            plain.inc()
            g.inc()
            g.dec(0.5)
            h.observe(0.001 * (k % 7))

    def scrape():
        start.wait()
        try:
            for _ in range(50):
                parse(r.exposition())          # consistent snapshots while counts move
        except Exception as exc:               # pragma: no cover - reported below
            errors.append(exc)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                # switch threads as often as possible
    try:
        threads = [threading.Thread(target=work, args=(i,)) for i in range(threads_n)]
        threads.append(threading.Thread(target=scrape))
        for t in threads:
            t.start()
        start.wait()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert not errors
    total = threads_n * per_thread
    assert plain.value == total
    assert sum(r.value("pdm_race_total", topic=f"t{i}") for i in range(3)) == total
    assert g.value == total * 0.5
    assert r.value("pdm_race_seconds_count") == total
    fam = parse(r.exposition())
    assert value(fam, "pdm_race_seconds", "pdm_race_seconds_bucket", le="+Inf") == total


# --- the catalogue and service_metrics -----------------------------------------------------------

def test_service_metrics_gives_the_standard_set_and_the_services_own():
    r = Registry()
    m = service_metrics("detector", r)
    for name in STANDARD:
        assert name in m
    assert set(SERVICE["detector"]) <= set(m.names)
    assert m.messages_in_total is r.get("pdm_messages_in_total")
    assert m.scoring_seconds.buckets[-2:] == (10.0, math.inf)
    assert r.value("pdm_up", service="detector") == 1
    assert r.value("pdm_process_start_time_seconds") == metrics.PROCESS_START
    assert r.service == "detector"
    again = service_metrics("detector", r)          # idempotent: the same metrics
    assert again.windows_scored_total is m.windows_scored_total
    m.set_up(False)
    assert r.value("pdm_up", service="detector") == 0
    plain = service_metrics("replayer", Registry())  # not in SERVICE: the standard set only
    assert set(plain.names) == set(STANDARD)


def test_handling_counts_times_and_reraises():
    r = Registry()
    m = service_metrics("gate", r)
    with m.handling("pdm/x/scores"):
        pass
    with pytest.raises(KeyError):
        with m.handling("pdm/x/scores"):
            raise KeyError("bad message")
    assert r.value("pdm_messages_in_total", topic="pdm/x/scores") == 2
    assert r.value("pdm_handler_errors_total", topic="pdm/x/scores") == 1
    assert r.value("pdm_handler_seconds_count", topic="pdm/x/scores") == 2


def test_catalogue_names_follow_prometheus_conventions():
    for name, spec in CATALOGUE.items():
        assert name.startswith("pdm_"), name
        assert spec.kind in ("counter", "gauge", "histogram"), name
        assert (spec.kind == "counter") == name.endswith("_total"), name
        if spec.kind == "histogram":
            assert name.endswith("_seconds"), name   # every histogram here is a duration
        assert "le" not in spec.labels and len(set(spec.labels)) == len(spec.labels), name
    # each catalogue entry can be created in a registry
    r = Registry()
    for name in CATALOGUE:
        metrics.metric(name, r)
    parse(r.exposition())
    with pytest.raises(KeyError, match="not in the metrics catalogue"):
        metrics.metric("pdm_made_up_total", r)


def test_port_for_reads_the_environment(monkeypatch):
    monkeypatch.delenv("PDM_METRICS_PORT", raising=False)
    assert metrics.port_for("ingest") == 9101
    assert metrics.port_for("replayer") is None
    monkeypatch.setenv("PDM_METRICS_PORT", "19999")
    assert metrics.port_for("ingest") == 19999
    monkeypatch.setenv("PDM_METRICS_PORT", "off")
    assert metrics.port_for("ingest") is None
    assert serve(Registry(), None) is None


# --- the HTTP endpoint ----------------------------------------------------------------------------

def _get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy
    try:
        with opener.open(url, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type"), err.read().decode("utf-8")


def test_server_serves_metrics_and_health_on_a_free_port():
    r = Registry()
    m = service_metrics("ingest", r)
    m.records_quarantined_total.labels(asset="a1", code="bad_json").inc()
    server = serve(r, 0, host="127.0.0.1")
    try:
        base = f"http://127.0.0.1:{server.port}"
        status, ctype, body = _get(base + "/metrics")
        assert status == 200
        assert ctype == "text/plain; version=0.0.4; charset=utf-8"
        fam = parse(body)
        assert value(fam, "pdm_records_quarantined_total", asset="a1", code="bad_json") == 1.0
        assert value(fam, "pdm_up", service="ingest") == 1.0
        status, ctype, body = _get(base + "/health")
        assert status == 200 and ctype == "application/json"
        doc = json.loads(body)
        assert doc["status"] == "ok" and doc["service"] == "ingest" and doc["uptime_s"] >= 0
        assert _get(base + "/nothing")[0] == 404
    finally:
        server.close()


def test_health_reports_unavailable_when_the_check_fails():
    state = {"ok": True}
    server = serve(Registry(), 0, host="127.0.0.1", service="agent", health=lambda: state["ok"])
    try:
        url = f"http://127.0.0.1:{server.port}/health"
        assert _get(url)[0] == 200
        state["ok"] = False
        status, _, body = _get(url)
        assert status == 503 and json.loads(body)["status"] == "unavailable"
        server.health = lambda: 1 / 0
        status, _, body = _get(url)
        assert status == 503 and "ZeroDivisionError" in json.loads(body)["error"]
    finally:
        server.close()


# --- dashboards and alert rules against the catalogue ------------------------------------------------

KEYWORDS = {"by", "without", "on", "ignoring", "group_left", "group_right", "and", "or", "unless", "bool",
            "offset", "inf", "nan"}


def metric_names(expr):
    """Metric names in a PromQL expression (good enough for the expressions in ops/)."""
    s = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)                        # string literals
    s = re.sub(r"\[[^\]]*\]", " ", s)                                    # [5m], [$__range]
    s = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", s)
    s = re.sub(r"\{[^}]*\}", " ", s)                                     # label matchers
    s = re.sub(r"\boffset\s+\S+", " ", s)
    s = re.sub(r"(?<![\w.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", " ", s)      # numbers
    names = set()
    for m in re.finditer(r"[A-Za-z_:][A-Za-z0-9_:]*", s):
        if s[m.end():].lstrip().startswith("(") or m.group(0).lower() in KEYWORDS:
            continue                                                     # a function or a keyword
        names.add(m.group(0))
    return names


def selectors(expr):
    """(metric, label names matched) for every metric{...} selector."""
    s = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    for m in re.finditer(r"([A-Za-z_:][A-Za-z0-9_:]*)\s*\{([^}]*)\}", s):
        yield m.group(1), set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)", m.group(2)))


def family(name):
    """The catalogue entry a sample name belongs to (histograms: _bucket, _sum, _count)."""
    if name in CATALOGUE:
        return name
    for suffix in ("_bucket", "_sum", "_count"):
        base = name[: -len(suffix)]
        if name.endswith(suffix) and base in CATALOGUE and CATALOGUE[base].kind == "histogram":
            return base
    return None


def test_metric_name_reader():
    expr = ('histogram_quantile(0.95, sum by (job, le) (rate(pdm_handler_seconds_bucket{job=~"$job"}[5m]))) '
            '> 2 and on(job) up == 1 or sum without (x) (pdm_up offset 5m) * 60')
    assert metric_names(expr) == {"pdm_handler_seconds_bucket", "up", "pdm_up"}
    assert list(selectors('a{b="c", d=~"e"} + f{g!="h"}')) == [("a", {"b", "d"}), ("f", {"g"})]


def panels(doc):
    for p in doc["panels"]:
        yield p
        yield from p.get("panels", [])            # panels inside collapsed rows


def dashboard_exprs(doc):
    for p in panels(doc):
        for t in p.get("targets", []):
            yield p["title"], t["expr"]


def alert_rules():
    doc = yaml.safe_load((OPS / "prometheus" / "alerts.yml").read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def all_exprs():
    for path in DASHBOARDS:
        doc = json.loads(path.read_text())
        for title, expr in dashboard_exprs(doc):
            yield f"{path.name}: {title}", expr
        for var in doc["templating"]["list"]:
            m = re.match(r"label_values\(\s*([A-Za-z_:][A-Za-z0-9_:]*)\s*,\s*(\w+)\s*\)", var["query"])
            assert m, var["query"]
            yield f"{path.name}: variable {var['name']}", f'{m.group(1)}{{{m.group(2)}=""}}'
    for rule in alert_rules():
        yield f"alerts.yml: {rule['alert']}", rule["expr"]


def test_dashboards_and_alerts_use_only_catalogue_metrics():
    assert {p.name for p in DASHBOARDS} == {"service_health.json", "model_health.json"}
    unknown = {}
    for where, expr in all_exprs():
        names = metric_names(expr)
        assert names, f"{where}: no metric in {expr!r}"
        bad = {n for n in names if family(n) is None and n not in BUILTIN}
        if bad:
            unknown[where] = sorted(bad)
    assert not unknown, f"metrics not in pdm_common.metrics: {unknown}"


def test_label_matchers_use_labels_the_metric_has():
    wrong = {}
    for where, expr in all_exprs():
        for name, keys in selectors(expr):
            if name in BUILTIN:
                allowed = {"job", "instance"}
            else:
                base = family(name)
                allowed = set(CATALOGUE[base].labels) | {"job", "instance"}
                if name.endswith("_bucket"):
                    allowed.add("le")
            if keys - allowed:
                wrong[f"{where}: {name}"] = sorted(keys - allowed)
    assert not wrong, wrong


def test_le_values_in_queries_are_bucket_bounds_the_client_writes():
    for where, expr in all_exprs():
        for name, le in re.findall(r'([A-Za-z_:][A-Za-z0-9_:]*_bucket)\{[^}]*le="([^"]+)"', expr):
            spec = CATALOGUE[family(name)]
            written = {metrics._fmt(b) for b in (spec.buckets or DEFAULT_BUCKETS)} | {"+Inf"}
            assert le in written, f"{where}: le={le!r} is not one of {sorted(written)}"


def test_dashboards_show_what_r35_and_ops02_ask_for():
    used = {}
    for path in DASHBOARDS:
        doc = json.loads(path.read_text())
        used[path.stem] = {family(n) or n for _, expr in dashboard_exprs(doc) for n in metric_names(expr)}
    assert {"up", "pdm_up", "pdm_messages_in_total", "pdm_messages_out_total", "pdm_handler_seconds",
            "pdm_queue_depth", "pdm_handler_errors_total", "pdm_records_quarantined_total"} <= used["service_health"]
    assert {"pdm_windows_scored_total", "pdm_windows_dropped_total", "pdm_alerts_total", "pdm_incidents_opened_total",
            "pdm_scoring_seconds", "pdm_agent_runs_total", "pdm_tool_calls_total", "pdm_brief_rejections_total",
            "pdm_ne107_status_changes_total", "pdm_drift_alerts_total"} <= used["model_health"]


def test_dashboards_are_loadable_json():
    datasources = yaml.safe_load((OPS / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text())
    prometheus_uid = next(d["uid"] for d in datasources["datasources"] if d["type"] == "prometheus")
    uids = set()
    for path in DASHBOARDS:
        doc = json.loads(path.read_text())
        assert doc.get("id") is None                       # Grafana assigns ids to provisioned dashboards
        assert doc["schemaVersion"] == 39 and doc["uid"] and doc["title"]
        uids.add(doc["uid"])
        ids = [p["id"] for p in panels(doc)]
        assert len(ids) == len(set(ids)), f"{path.name}: duplicate panel ids"
        for p in panels(doc):
            pos = p["gridPos"]
            assert pos["x"] + pos["w"] <= 24 and pos["w"] > 0 and pos["h"] > 0, (path.name, p["title"])
            refs = [t["refId"] for t in p.get("targets", [])]
            assert len(refs) == len(set(refs)), (path.name, p["title"])
            for t in p.get("targets", []):
                assert t["datasource"]["uid"] == prometheus_uid and t["expr"].strip()
            if p["type"] != "text":
                assert p["datasource"]["uid"] == prometheus_uid
                assert p.get("targets"), (path.name, p["title"])
    assert len(uids) == len(DASHBOARDS)
    provider = yaml.safe_load((OPS / "grafana" / "provisioning" / "dashboards" / "dashboards.yml").read_text())
    assert provider["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"


def test_prometheus_scrapes_every_service_at_its_port():
    config = yaml.safe_load((OPS / "prometheus" / "prometheus.yml").read_text())
    assert config["global"]["scrape_interval"] == "5s"
    jobs = {job["job_name"]: [t for sc in job["static_configs"] for t in sc["targets"]]
            for job in config["scrape_configs"]}
    for service, port in PORTS.items():
        assert jobs.get(service) == [f"{service}:{port}"], service
    assert set(jobs) == set(PORTS) | {"prometheus"}
    assert config["rule_files"] == ["alerts.yml"]
    assert len(set(PORTS.values())) == len(PORTS)


def test_alert_rules_cover_down_latency_quarantine_and_briefs():
    rules = {r["alert"]: r for r in alert_rules()}
    assert set(rules) == {"PdmServiceDown", "PdmScoringLatencyHigh", "PdmQuarantineRateRising", "PdmBriefRejections"}
    for name, rule in rules.items():
        assert rule["labels"]["severity"] in ("critical", "warning", "info"), name
        assert rule["labels"]["area"] in ("service", "model"), name
        assert rule["annotations"]["summary"], name
    assert "> 2" in rules["PdmScoringLatencyHigh"]["expr"]
    assert "pdm_scoring_seconds_bucket" in rules["PdmScoringLatencyHigh"]["expr"]


def test_collector_receives_otlp_and_exports_to_jaeger():
    config = yaml.safe_load((OPS / "otel" / "collector.yml").read_text())
    protocols = config["receivers"]["otlp"]["protocols"]
    assert protocols["grpc"]["endpoint"].endswith(":4317")
    assert protocols["http"]["endpoint"].endswith(":4318")
    pipeline = config["service"]["pipelines"]["traces"]
    assert pipeline["receivers"] == ["otlp"]
    assert "batch" in pipeline["processors"]
    exporter = pipeline["exporters"][0]
    assert exporter.startswith("otlp") and config["exporters"][exporter]["endpoint"] == "jaeger:4317"
