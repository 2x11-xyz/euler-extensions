//! Pure-logic unit tests ported from the bundled `euler-extension-maxproof`
//! crate. The bundled tests drove a `MockHost` that simply returned a fixed
//! event list and captured the artifact write; here the command bodies are
//! pure over their already-fetched provenance events, so the tests call them
//! directly with events built as wire JSON. Provenance-query mapping is
//! asserted through `provenance_query`, and the tournament artifact through the
//! returned `TournamentReport`.

use super::*;

// ---------------------------------------------------------------------------
// Command drivers.
// ---------------------------------------------------------------------------

fn population(input: Value) -> Result<Value, Error> {
    population_brief(&PopulationInput::parse(&input)?)
}

fn verify(input: Value, events: &[Value]) -> Result<Value, Error> {
    verify_brief(&VerifyInput::parse(&input)?, events)
}

fn verify_query(input: &Value) -> ProvenanceQuery {
    provenance_query(
        &VerifyInput::parse(input)
            .expect("verify input")
            .query_window,
    )
}

fn tournament(input: Value, events: &[Value]) -> Result<TournamentReport, Error> {
    tournament_report(&TournamentInput::parse(&input)?, events)
}

fn artifact_of(report: &TournamentReport) -> Value {
    serde_json::from_slice(&report.bytes).expect("artifact bytes")
}

fn tournament_query(input: &Value) -> ProvenanceQuery {
    provenance_query(
        &TournamentInput::parse(input)
            .expect("tournament input")
            .query_window,
    )
}

// ---------------------------------------------------------------------------
// population-brief.
// ---------------------------------------------------------------------------

#[test]
fn population_brief_accepts_default_min_and_max_sizes() {
    let default_output = population(json!({"problem":"prove 1+1=2"})).expect("default brief");
    assert_eq!(default_output["population_size"], json!(4));

    for requested in [1, 8] {
        let output = population(
            json!({"problem":"prove 1+1=2","population_size":requested,"max_tokens":2048}),
        )
        .expect("population brief");
        let briefs = output["briefs"].as_array().expect("briefs");

        assert_eq!(briefs.len(), requested);
        assert_eq!(output["population_size"], json!(requested));
        assert_eq!(briefs[0]["persona"], json!("maxproof-generator-0"));
        assert_eq!(briefs[0]["provider"], json!(""));
        assert_eq!(briefs[0]["model"], json!(""));
        assert_eq!(briefs[0]["capabilities"], json!([]));
        assert_eq!(briefs[0]["budget"]["max_turns"], json!(1));
        assert_eq!(briefs[0]["budget"]["max_tool_calls"], json!(0));
        assert_eq!(briefs[0]["budget"]["max_tokens"], json!(2048));
        assert!(briefs[0]["system_prompt"]
            .as_str()
            .expect("system prompt")
            .contains(CANDIDATE_SCHEMA));
    }
}

#[test]
fn population_brief_rejects_out_of_range_population_size() {
    for requested in [0, 9] {
        let error = population(json!({"problem":"prove 1+1=2","population_size":requested}))
            .expect_err("population_size out of range");
        assert!(error
            .to_string()
            .contains("population_size must be in range 1..=8"));
    }
}

#[test]
fn population_brief_rejects_bad_inputs() {
    let oversized = "x".repeat(MAX_PROBLEM_BYTES + 1);
    let oversized_error = population(json!({"problem": oversized})).expect_err("oversized problem");
    assert!(oversized_error
        .to_string()
        .contains("problem exceeds maximum"));

    let unknown_error =
        population(json!({"problem":"p","path":"/tmp/nope"})).expect_err("unknown key");
    assert!(unknown_error
        .to_string()
        .contains("unknown input field `path`"));
}

// ---------------------------------------------------------------------------
// verify-brief.
// ---------------------------------------------------------------------------

#[test]
fn verify_brief_records_malformed_candidate_without_crashing() {
    let (spawn, result) = spawn_and_result("candidate-1", "not json");
    let output = verify(
        json!({"candidate_spawn_event_ids":["candidate-1"]}),
        &[spawn, result],
    )
    .expect("verify output");

    assert_eq!(output["briefs"], json!([]));
    assert_eq!(
        output["candidate_failures"][0]["candidate_spawn_event_id"],
        "candidate-1"
    );
    assert!(output["candidate_failures"][0]["reason"]
        .as_str()
        .expect("reason")
        .contains("candidate output is not JSON"));
}

#[test]
fn verify_brief_reports_candidate_output_context_for_unknown_fields() {
    let candidate = json!({
        "schema": CANDIDATE_SCHEMA,
        "proof": "proof body",
        "approach_summary": "summary",
        "claimed_confidence": "high",
        "extra": true,
    })
    .to_string();
    let (spawn, result) = spawn_and_result("candidate-1", &candidate);
    let output = verify(
        json!({"candidate_spawn_event_ids":["candidate-1"]}),
        &[spawn, result],
    )
    .expect("verify output");

    assert!(output["candidate_failures"][0]["reason"]
        .as_str()
        .expect("reason")
        .contains("unknown field `extra` in candidate output"));
}

#[test]
fn verify_brief_rejects_empty_candidate_array_and_unpaired_id() {
    let empty_error = verify(json!({"candidate_spawn_event_ids":[]}), &[]).expect_err("empty ids");
    assert!(empty_error
        .to_string()
        .contains("candidate_spawn_event_ids must not be empty"));

    let unpaired_error = verify(json!({"candidate_spawn_event_ids":["missing-spawn"]}), &[])
        .expect_err("unpaired id");
    assert!(unpaired_error.to_string().contains("missing-spawn"));
    assert!(unpaired_error.to_string().contains("widen the window"));
}

#[test]
fn verify_brief_emits_independent_verifier_task_for_valid_candidate() {
    let (spawn, result) = spawn_and_result("candidate-1", &candidate_json("short approach"));
    let output = verify(
        json!({"candidate_spawn_event_ids":["candidate-1"]}),
        &[spawn, result],
    )
    .expect("verify output");
    let brief = &output["briefs"][0];

    assert_eq!(brief["candidate_spawn_event_id"], json!("candidate-1"));
    assert_eq!(brief["persona"], json!("maxproof-verifier"));
    assert!(brief["task"].as_str().expect("task").contains("Problem:"));
    assert!(brief["task"].as_str().expect("task").contains(PROOF_BEGIN));
    assert!(brief["task"].as_str().expect("task").contains("proof body"));
    assert!(brief["system_prompt"]
        .as_str()
        .expect("system prompt")
        .contains("Ignore the candidate's claimed_confidence entirely"));
}

#[test]
fn verify_brief_uses_window_inputs_and_verifier_budget() {
    let (spawn, result) = spawn_and_result("candidate-1", &candidate_json("short approach"));
    let input = json!({
        "candidate_spawn_event_ids":["candidate-1"],
        "limit": 17,
        "scan_limit": 23,
        "after_event_id": "event-before",
        "max_tokens": 4096,
    });
    let query = verify_query(&input);
    let output = verify(input, &[spawn, result]).expect("verify output");

    assert_eq!(query.limit, 17);
    assert_eq!(query.scan_limit, 23);
    assert_eq!(query.after_event_id, Some("event-before".to_owned()));
    assert_eq!(output["briefs"][0]["budget"]["max_tokens"], json!(4096));
}

// ---------------------------------------------------------------------------
// tournament.
// ---------------------------------------------------------------------------

#[test]
fn tournament_downgrades_correct_verdict_with_fatal_error() {
    let events = vec![
        candidate_spawn("candidate-1", "prove 1+1=2", "proof body"),
        result("candidate-1", &candidate_json("approach")),
        verifier_spawn("verdict-1", "prove 1+1=2", "proof body"),
        result("verdict-1", &verdict_json("correct", &["fatal"])),
    ];
    let input = json!({
        "pairs":[{"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-1"}],
        "limit": 19,
        "scan_limit": 29,
        "after_event_id": "event-before",
    });
    let query = tournament_query(&input);
    let report = tournament(input, &events).expect("tournament");
    let artifact = artifact_of(&report);

    assert_eq!(query.limit, 19);
    assert_eq!(query.scan_limit, 29);
    assert_eq!(query.after_event_id, Some("event-before".to_owned()));
    assert_eq!(report.winner_spawn_event_id, "candidate-1");
    assert_eq!(artifact["population"][0]["fitness"], json!(0));
    assert_eq!(artifact["population"][0]["downgraded"], json!(true));
    assert_eq!(artifact["population"][0]["error_counts"]["fatal"], json!(1));
}

#[test]
fn tournament_unknown_severity_is_structured_validation_error() {
    let events = vec![
        candidate_spawn("candidate-1", "prove 1+1=2", "proof body"),
        result("candidate-1", &candidate_json("approach")),
        verifier_spawn("verdict-1", "prove 1+1=2", "proof body"),
        result("verdict-1", &verdict_json("incorrect", &["critical"])),
    ];
    let report = tournament(
        json!({"pairs":[{"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-1"}]}),
        &events,
    )
    .expect("tournament with invalid verdict");
    let artifact = artifact_of(&report);

    assert_eq!(artifact["population"][0]["fitness"], json!(0));
    assert!(artifact["population"][0]["validation_error"]
        .as_str()
        .expect("validation error")
        .contains("unknown severity `critical`"));
}

#[test]
fn tournament_tie_breaks_by_error_count_then_first_listed() {
    let events = vec![
        candidate_spawn("candidate-a", "prove 1+1=2", "proof body"),
        result("candidate-a", &candidate_json("approach a")),
        verifier_spawn("verdict-a", "prove 1+1=2", "proof body"),
        result(
            "verdict-a",
            &verdict_json("incomplete", &["minor", "minor"]),
        ),
        candidate_spawn("candidate-b", "prove 1+1=2", "proof body"),
        result("candidate-b", &candidate_json("approach b")),
        verifier_spawn("verdict-b", "prove 1+1=2", "proof body"),
        result("verdict-b", &verdict_json("incomplete", &["minor"])),
        candidate_spawn("candidate-c", "prove 1+1=2", "proof body"),
        result("candidate-c", &candidate_json("approach c")),
        verifier_spawn("verdict-c", "prove 1+1=2", "proof body"),
        result("verdict-c", &verdict_json("incomplete", &["minor"])),
    ];
    let report = tournament(
        json!({"pairs":[
            {"candidate_spawn_event_id":"candidate-a","verdict_spawn_event_id":"verdict-a"},
            {"candidate_spawn_event_id":"candidate-b","verdict_spawn_event_id":"verdict-b"},
            {"candidate_spawn_event_id":"candidate-c","verdict_spawn_event_id":"verdict-c"}
        ]}),
        &events,
    )
    .expect("tournament");

    assert_eq!(report.winner_spawn_event_id, "candidate-b");
}

#[test]
fn tournament_rejects_duplicate_candidate_or_verdict_ids() {
    let duplicate_candidate = tournament(
        json!({"pairs":[
            {"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-1"},
            {"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-2"}
        ]}),
        &[],
    )
    .expect_err("duplicate candidate");
    assert!(duplicate_candidate
        .to_string()
        .contains("duplicate candidate_spawn_event_id `candidate-1`"));

    let duplicate_verdict = tournament(
        json!({"pairs":[
            {"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-1"},
            {"candidate_spawn_event_id":"candidate-2","verdict_spawn_event_id":"verdict-1"}
        ]}),
        &[],
    )
    .expect_err("duplicate verdict");
    assert!(duplicate_verdict
        .to_string()
        .contains("duplicate verdict_spawn_event_id `verdict-1`"));
}

#[test]
fn tournament_confirms_two_distinct_fitness_two_candidates() {
    let events = vec![
        candidate_spawn("candidate-a", "prove 1+1=2", "proof body"),
        result("candidate-a", &candidate_json_with_proof("proof body", "a")),
        verifier_spawn("verdict-a", "prove 1+1=2", "proof body"),
        result("verdict-a", &verdict_json("correct", &[])),
        candidate_spawn("candidate-b", "prove 1+1=2", "second proof"),
        result(
            "candidate-b",
            &candidate_json_with_proof("second proof", "b"),
        ),
        verifier_spawn("verdict-b", "prove 1+1=2", "second proof"),
        result("verdict-b", &verdict_json("correct", &[])),
    ];
    let report = tournament(
        json!({"pairs":[
            {"candidate_spawn_event_id":"candidate-a","verdict_spawn_event_id":"verdict-a"},
            {"candidate_spawn_event_id":"candidate-b","verdict_spawn_event_id":"verdict-b"}
        ]}),
        &events,
    )
    .expect("tournament");

    assert_eq!(report.independent_confirmations, 2);
    assert_eq!(
        early_stop_confidence(report.independent_confirmations),
        "confirmed"
    );
}

#[test]
fn tournament_rejects_wrong_personas() {
    let bad_candidate_events = vec![
        spawn_with_persona(
            "candidate-1",
            "ordinary-worker",
            "prove 1+1=2",
            "proof body",
        ),
        result("candidate-1", &candidate_json("approach")),
        verifier_spawn("verdict-1", "prove 1+1=2", "proof body"),
        result("verdict-1", &verdict_json("correct", &[])),
    ];
    let bad_candidate = tournament(
        json!({"pairs":[{"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-1"}]}),
        &bad_candidate_events,
    )
    .expect_err("candidate persona");
    assert!(bad_candidate
        .to_string()
        .contains("candidate spawn_event_id `candidate-1` persona must start"));

    let bad_verdict_events = vec![
        candidate_spawn("candidate-1", "prove 1+1=2", "proof body"),
        result("candidate-1", &candidate_json("approach")),
        candidate_spawn("verdict-1", "prove 1+1=2", "proof body"),
        result("verdict-1", &verdict_json("correct", &[])),
    ];
    let bad_verdict = tournament(
        json!({"pairs":[{"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-1"}]}),
        &bad_verdict_events,
    )
    .expect_err("verdict persona");
    assert!(bad_verdict
        .to_string()
        .contains("verdict spawn_event_id `verdict-1` persona must be `maxproof-verifier`"));
}

#[test]
fn tournament_rejects_verdict_bound_to_different_proof() {
    let events = vec![
        candidate_spawn("candidate-1", "prove 1+1=2", "proof body"),
        result("candidate-1", &candidate_json("approach")),
        verifier_spawn("verdict-1", "prove 1+1=2", "different proof"),
        result("verdict-1", &verdict_json("correct", &[])),
    ];
    let error = tournament(
        json!({"pairs":[{"candidate_spawn_event_id":"candidate-1","verdict_spawn_event_id":"verdict-1"}]}),
        &events,
    )
    .expect_err("proof mismatch");

    assert!(error.to_string().contains(
        "verdict_spawn_event_id `verdict-1` proof digest does not match candidate_spawn_event_id `candidate-1`"
    ));
}

#[test]
fn tournament_rejects_mixed_problem_digests() {
    let events = vec![
        candidate_spawn("candidate-a", "problem a", "proof body"),
        result("candidate-a", &candidate_json("a")),
        verifier_spawn("verdict-a", "problem a", "proof body"),
        result("verdict-a", &verdict_json("correct", &[])),
        candidate_spawn("candidate-b", "problem b", "proof body"),
        result("candidate-b", &candidate_json("b")),
        verifier_spawn("verdict-b", "problem b", "proof body"),
        result("verdict-b", &verdict_json("correct", &[])),
    ];
    let error = tournament(
        json!({"pairs":[
            {"candidate_spawn_event_id":"candidate-a","verdict_spawn_event_id":"verdict-a"},
            {"candidate_spawn_event_id":"candidate-b","verdict_spawn_event_id":"verdict-b"}
        ]}),
        &events,
    )
    .expect_err("mixed problems");

    assert!(error
        .to_string()
        .contains("mixed problem digests in tournament pairs"));
}

#[test]
fn missing_agent_result_parent_is_rejected() {
    let events = vec![
        candidate_spawn("candidate-1", "prove 1+1=2", "proof body"),
        result_with_parent("candidate-1", None, &candidate_json("approach")),
    ];
    let error = verify(
        json!({"candidate_spawn_event_ids":["candidate-1"]}),
        &events,
    )
    .expect_err("missing parent");

    assert!(error
        .to_string()
        .contains("agent.result result-candidate-1 parent must be `candidate-1`"));
}

// ---------------------------------------------------------------------------
// Wire-event builders (mirror the bundled EventEnvelope fixtures).
// ---------------------------------------------------------------------------

fn spawn_and_result(id: &str, output: &str) -> (Value, Value) {
    (
        candidate_spawn(id, "prove 1+1=2", "proof body"),
        result(id, output),
    )
}

fn candidate_spawn(id: &str, problem: &str, proof: &str) -> Value {
    spawn_with_persona(id, "maxproof-generator-0", problem, proof)
}

fn verifier_spawn(id: &str, problem: &str, proof: &str) -> Value {
    event(
        id,
        None,
        AGENT_SPAWN,
        json!({
            "persona": VERIFIER_PERSONA,
            "task": verifier_task(problem, proof),
        }),
    )
}

fn spawn_with_persona(id: &str, persona: &str, problem: &str, _proof: &str) -> Value {
    event(
        id,
        None,
        AGENT_SPAWN,
        json!({
            "persona": persona,
            "task": generator_task(problem, "direct proof"),
        }),
    )
}

fn result(spawn_id: &str, output: &str) -> Value {
    result_with_parent(spawn_id, Some(spawn_id), output)
}

fn result_with_parent(spawn_id: &str, parent: Option<&str>, output: &str) -> Value {
    event(
        &format!("result-{spawn_id}"),
        parent,
        AGENT_RESULT,
        json!({
            "spawn_event_id": spawn_id,
            "ok": true,
            "summary": "done",
            "output": output,
        }),
    )
}

fn event(id: &str, parent: Option<&str>, kind: &str, payload: Value) -> Value {
    json!({
        "v": 1,
        "id": id,
        "ts": "2026-07-05T00:00:00.000Z",
        "session": "session",
        "agent": "agent",
        "parent": parent,
        "kind": kind,
        "payload": payload,
        "blobs": {},
    })
}

fn candidate_json(summary: &str) -> String {
    candidate_json_with_proof("proof body", summary)
}

fn candidate_json_with_proof(proof: &str, summary: &str) -> String {
    json!({
        "schema": CANDIDATE_SCHEMA,
        "proof": proof,
        "approach_summary": summary,
        "claimed_confidence": "high",
    })
    .to_string()
}

fn verdict_json(verdict: &str, severities: &[&str]) -> String {
    let errors = severities
        .iter()
        .map(|severity| json!({"location":"line 1","description":"issue","severity": severity}))
        .collect::<Vec<_>>();
    json!({
        "schema": VERDICT_SCHEMA,
        "assessment": "checked",
        "errors": errors,
        "verdict": verdict,
    })
    .to_string()
}
