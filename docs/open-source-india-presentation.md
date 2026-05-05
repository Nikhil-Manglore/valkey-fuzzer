# Valkey Fuzzer — Briefing Document for Open Source India Talk

1. What is Valkey (10 Minutes)

Note: All the information you need for this section is in this video: https://www.youtube.com/watch?v=P6Cb0diRzdE.

Valkey is an open source, in-memory, key-value data store licensed under BSD 3-Clause. The project was created in March 2024, after Redis Inc. changed the license of Redis OSS from BSD 3-Clause to a dual source-available model (RSALv2 and SSPLv1) that is not considered open source by the Open Source Initiative. A group of longtime Redis maintainers and contributors from AWS, Google Cloud, Oracle, Ericsson, and Snap forked Redis 7.2.4 under BSD 3-Clause and announced the project within days of the license change. The Linux Foundation announced it would host the project on March 28, 2024. As of the one-year mark, the project had contributions from roughly fifty companies and more than one thousand commits from around one hundred and fifty contributors

Valkey is a high-performance, in-memory data structure store that supports over 200 commands. It keeps data in memory, offers microsecond-latency reads and writes, and supports rich native data types including strings, hashes, lists, sets, sorted sets, bitmaps, HyperLogLogs, geospatial indexes, and streams. These data types make Valkey useful in a wide range of roles, ranging from caches, session stores, rate limiters, leaderboards, pub/sub and streaming message brokers, feature flag stores, and even primary databases for workloads that fit in memory.

The simplest Valkey deployment is standalone mode. In standalone mode there is one primary node and one or more replica nodes for availability. All writes flow through the primary. This configuration is bounded by the CPU, memory, and network interface of a single machine. It is sufficient for many workloads but does not scale horizontally. Standalone mode also has limited fault tolerance since if the primary goes down, writes fail until a replica takes over, and if the replica has not received all the data from the primary, some data can be lost.

Cluster mode was created to solve those limitations. It is Valkey's horizontally scalable deployment model and is the target of our fuzzer. Cluster mode supports multiple primaries, and data is distributed across them, so capacity grows with the number of nodes rather than with the size of a single machine. Cluster mode also provides automatic fault tolerance: if a primary goes down, the cluster detects the failure and promotes one of its replicas to take over, keeping the affected slots available with minimal data loss.

In cluster mode, the keyspace is divided into exactly 16,384 hash slots. When a client writes a key, Valkey computes CRC16 of the key modulo 16,384 to determine its slot. Each slot is owned by exactly one primary node, and each primary may have zero or more replicas holding a copy of its data. Slots are distributed across primaries; in a three-shard cluster, for example, primary 0 owns slots 0–5460, primary 1 owns 5461–10922, and primary 2 owns 10923–16383.

Clients in cluster mode are cluster-aware. They compute the target slot locally, look up the owning node, and send the command directly to that node. Slots do not always stay on the node that they started on. An operator can reshard to rebalance load across primaries, add new shards (more primary nodes to hold data) and migrate slots onto them, or move hot slots off a busy primary. Additionally a failover transfers slot ownership from a dead primary to its promoted replica. If the client contacts the wrong node after a slot has moved, the node responds with a MOVED or ASK redirect pointing to the correct owner, and the client updates its routing table. There is no proxy, no central coordinator, and no node that sees all traffic. This is what enables horizontal scaling to thousands of nodes. Slots move to other nodes in a Valkey cluster. 

Cluster coordination is handled directly between the nodes themselves over a mechanism known as the cluster bus. The cluster bus is a full mesh of persistent TCP connections and every node maintains a connection to every other node. It is over these connections that nodes exchange small gossip messages. Four categories of information flow across the cluster bus: membership discovery, heartbeats, failure agreement, and failover coordination. Membership discovery happens when a node joins the cluster by sending a MEET message to any existing member, after which membership information propagates through gossip until every node has learned about the new node. Heartbeats flow continuously, with every node pinging every other node on a regular cadence, and if a node fails to respond beyond the configured cluster-node-timeout, its peers mark it PFAIL ("potentially failed"). PFAIL is just a local suspicion from another node in the cluster, not a cluster-wide decision. Failure agreement upgrades a PFAIL to FAIL once a quorum (majority) of primaries independently observes the same node as PFAIL. Failover then begins. Once a primary is declared FAIL, its replicas run an election, each requesting votes from the surviving primaries, and the winner is promoted as the new primary. Clients that are addressed to the old primary will be redirected to the new one via MOVED or ASK replies. All of this runs over gossip and the correctness of the cluster bus is what keeps the data correct.

Scale context: the 1 billion RPS benchmark

[Note: The October 2025 blog post "Scaling a Valkey Cluster to 1 Billion Requests per Second" (https://valkey.io/blog/1-billion-rps/) is the primary reference for understanding why cluster bus correctness is difficult at scale]

The benchmark ran a 2,000-node Valkey cluster on AWS r7g.2xlarge instances (8 cores, 64 GB memory per node), driven by 750 c7g.16xlarge client machines. It sustained over one billion SET operations per second. Scaling to 2,000 nodes with bounded recovery time is a feature of the Valkey 9.0 release, and reaching that scale required fixing several bugs in the cluster bus that only manifest under failure at scale. Three are illustrative of the problem class our fuzzer targets:

Collision during multi-primary failover. Failover is serialized by design — only one shard fails over at a time. When many primaries died simultaneously, replicas from different shards campaigned for election concurrently, vote requests collided on the surviving primaries, votes split, and no replica could win. The cluster could not heal without manual intervention. This was fixed in Valkey 8.1 by introducing a deterministic ranking based on shard ID, so shards queue up and fail over in order.

Reconnect storm to dead nodes. Every 100ms, each node attempts to reconnect to peers it has lost. In a cluster with hundreds of dead nodes, surviving nodes burned substantial CPU on sockets that would never open, starving healthy traffic. This was fixed by adding a throttling mechanism that bounds reconnect attempts within a configured node timeout.

Failure report overhead. After 499 of 2,000 nodes were killed, the surviving 1,501 continued to gossip about each failed node and exchange failure reports long after those nodes were already marked failed. Processing and cleaning up the duplicates consumed significant CPU. This was fixed by restructuring failure reports in a radix tree keyed on one-second time buckets, grouping reports efficiently and making expiration cheap.

Why this is relevant to the fuzzer

The three bugs above share a structural pattern since they manifest only at scale, under simultaneous or near-simultaneous failures, and only when event timing aligns. None of these failures would be caught by a unit test or a single-scenario integration test. That is the gap our fuzzer is designed to fill.


2. Why we built a fuzzer (5 Minutes)

Over time we have seen a steady stream of cluster-related bugs in both open source Valkey and Amazon ElastiCache. Even with extensive testing, many of these bugs slipped through because they are hard to reproduce with integration tests alone. The kinds of things that can go wrong include a shard ending up with two primaries after a sequence of node failures and restarts, a replication loop caused by stale gossip packets arriving out of order during a failover, slots getting stuck in an importing state after scale-up and rebalance under load, primaries with no slots assigned being counted toward the failure-detection quorum, a temporary shard ID becoming permanent in a node's config when the engine crashes mid-setup, and surviving nodes burning enough CPU on reconnection attempts to dead peers that they starve healthy traffic.

Each of these is a distributed-systems bug. They are non-deterministic, they arise from many things happening at once, and several of them describe scenarios that nobody wrote an explicit test for. Testing every possible cluster state by writing tests is impossible, but we can randomly generate scenarios built from the real cluster operations and real-world failure conditions that produce bugs like these. We can also keep running those scenarios until something breaks.

Fuzzing is well suited for testing the cluster bus. Instead of one scenario, we generate hundreds of random ones, with different cluster sizes, different operation orders, and different chaos patterns. Instead of one thing at a time, we deliberately overlap operations. We start a failover, kill another node mid-failover, and repeat with different timings until something breaks. Every fuzzer run is driven by a single integer seed. If seed 42 finds a bug, running with seed 42 again reproduces the exact same scenario, operation order, and chaos timing. That is what turns a flaky failure into a real bug report a developer can fix.

The goal of the Cluster Bus Fuzzer is to automate this kind of testing. We will randomly choose cluster operations and chaos events, run them against a real cluster, and verify that the cluster bus holds up under all of it. The specific pain point that pushed us to build this is that large-scale cluster bugs were being found by customers running Valkey in production and this led to data loss. There was no stage between integration tests and a customer's production deployment where we could reliably surface these bugs. The fuzzer fills that gap.


3. Alternatives we considered (2 Minutes)

Valkey already has a very solid test suite written in TCL. There are unit tests for data structures, integration tests for cluster formation, and scenario tests for specific failover paths. All of it is necessary, and all of it is good at what it does. But it does not catch the class of bugs described above. The TCL suite has three properties that are great for correctness but bad for distributed systems since the tests are deterministic and run the same thing every time. Distributed systems bugs are non-deterministic, come from many things happening at once, and are hard to imagine before they happen.


4. What the fuzzer does and the demo (15 Minutes - 7 for explanation and 8 for live demo)

The Valkey Fuzzer provisions a real Valkey cluster on a single machine, executes randomized cluster operations against it, injects coordinated failures while those operations are in flight, and then validates that the cluster remains in a correct state.

Each test run follows the same shape. The fuzzer either generates a random scenario from a seed or reads a scripted one from a YAML file that a user can create. Before starting the scenario the fuzzer will randomly generate the number of primaries and replicas per primary. It will also randomly generate the number of operations, the type of operation, and which nodes in the cluster the operation will affect. For each operation there will be one chaos operation associated. The fuzzer will randomly generate which node the chaos just affect. Currently we only support one time of operation (Failover) and one type of chaos (Process Kill). Finally the fuzzer will randomly generate timings for each of the operation and chaos pair. For example, all operations and chaos will run in parallel but some operations might start after a certain amount of time. Additionally, since each chaos operartion is paired with a cluster operation, the fuzzer will randomly decide whether the chaos should be injected before, during, or after the operation has completed. After all of these configurations are genarated, the Valkey-Fuzzer spins up a fresh Valkey cluster of the configured size, waits for the cluster to report healthy, and starts a real workload against it using `valkey-benchmark` so that all the nodes have data on them. It then works through the sequence of cluster operations that were randomly decided. The coordination between the operation and the chaos is what we mainly want to test since an idle cluster is easy to keep consistent. A cluster under load, mid-failover, while a replica is being killed, is where bugs can be found. After every operation and at the end of the run, the fuzzer checks if the cluster still correct. When the test finishes, or if anything fails, the cluster is torn down cleanly so the next run starts from a fresh state.

The seven validation checks are the most important part of the whole system since that will allow us to know if the cluster returned to a healthy state after we ran our test on it. 

1. Cluster status queries CLUSTER INFO on every node and confirms cluster_state is ok and quorum is present. 
2. Slot coverage confirms all 16,384 slots are assigned to a live primary and zero assigned to nodes we just killed. 
3. Topology confirms the actual primary and replica counts match what we configured, accounting for deliberately killed nodes. This allows us to know that the cluster sees all the nodes it expects after all operations and chaos are run.
4. Replication confirms every replica is connected to its primary and replication lag is below threshold. This is important because no data will be lost in case a primary fails in the future.
5. View consistency confirms every node agrees on cluster topology. To do this we query CLUSTER NODES on every node and check the difference between them. If node A thinks B is primary while node C thinks B is replica, that is split-brain. 
6. Data consistency writes known test keys before chaos and reads them back after in order to make sure no data was lost. 
7. Log validation checks the actual Valkey node log files for error-level messages indicating internal invariants were violated, catching bugs that do not surface through the client API at all. 
A test fails if any check fails.

The bug classes the fuzzer is designed to catch fall into five categories. 
1. Stuck failovers, where a primary is killed but no replica gets promoted, caught by slot coverage because the dead primary's slots remain assigned to a dead node. 
2. Orphaned slots, where a failover completes but some slots do not transfer correctly, also caught by slot coverage. 
3. Split-brain topology, where different nodes see different primaries for the same shard, caught by view consistency. 
4. Silent data loss, where a write was acknowledged, the primary died, and the new primary does not have the write.
6. And violated internal invariants, things like PANIC or assertion failed in the Valkey log, caught by log validation. 

As mentioned before users can also create their own scenario instead of relying on randomly generated scenarios. 
Scenarios are defined in human-readable YAML, also known as DSL (Domain Specific Language). Every random scenario can be exported to this format. A minimal example from examples/simple_failover.yaml:

```yaml
scenario_id: "simple-failover-test"
seed: 12345

cluster:
  num_shards: 3
  replicas_per_shard: 1
  base_port: 7000

operations:
  - type: "failover"
    target_node: "shard-0-primary"
    parameters:
      force: false
    timing:
      delay_before: 0.0
      timeout: 30.0
      delay_after: 0.0

chaos:
  type: "process_kill"
  target_selection:
    strategy: "random"
  timing:
    delay_before_operation: 0.0
    delay_after_operation: 0.0
    chaos_duration: 0.0
  coordination:
    chaos_before_operation: false
    chaos_during_operation: false
    chaos_after_operation: false

state_validation:
  check_slot_coverage: true
  check_cluster_status: true
  check_replication: true
  check_topology: true
  check_view_consistency: true
  check_data_consistency: false
  stabilization_wait: 5.0
  validation_timeout: 30.0
  cluster_status_config:
    acceptable_states: ['ok', 'unknown']
  replication_config:
    max_acceptable_lag: 15.0
```


For the demo itself, these are the exact commands in recommended order, each demonstrating a specific capability. 

valkey-fuzzer cluster --dsl examples/simple_failover.yaml --verbose will run a scripted scenario with no randomness. It spawns six Valkey processes (three primaries and three replicas), runs one failover, validates, and tears down in around thirty seconds. Good for proving the tool works end-to-end. 

A reproducible random test is `valkey-fuzzer cluster --seed 42 --verbose`, which shows off randomization and seeding. The header prints "Seed: 42 (reproducible)", and running this twice produces the same scenario, same operations, same chaos timing. 

valkey-fuzzer cluster --random will run a randomized test

Optional: 
To export a random scenario to DSL, run `valkey-fuzzer cluster --seed 42 --export-dsl /tmp/seed42.yaml` and then `cat /tmp/seed42.yaml`. That takes the random scenario from seed 42 and writes it as human-readable YAML, which a developer can commit to git, attach to a bug report, or edit by hand. 


What a failure report looks like, from the README:

```
=== Test Failure Report ===
Scenario: 4226257627
Status: FAILED
Duration: 211.85s
Operations: 2
Chaos Events: 2
Seed: 4226257627 (use to reproduce)

Final Validation: FAILED
Failed Checks: slot_coverage, topology, data_consistency

  Slot Coverage: FAIL
    → CRITICAL: 5461 slots still assigned to killed nodes.
      Killed nodes: {'127.0.0.1:7623', '127.0.0.1:7622'}.
  Topology: FAIL
    → Topology validation failed in strict mode: 2 mismatch(es) found
  Data Consistency: FAIL
    → 35 test key(s) unreachable (threshold: 10)

Reproduction Command:
valkey-fuzzer cluster --seed 4226257627
```


The fuzzer is mainly utilized via a reusable GitHub Actions workflow at https://github.com/valkey-io/valkey-fuzzer/actions. This workflow clones the unstable branch of Valkey, builds it, and then runs a randomly generated scenario on it. Upon finishing it uploads per-run artifacts including console logs and node logs and fails the workflow if any run fails. Failed runs are what Valkey contributors look into the most in order to see if there are any bugs. There is a scheduled run every four hours against the Valkey unstable branch.

5. AI Agent (5 Minutes)

The final report for every fuzzer run is faily long. Each run produces a lot of information such as the fuzzer's own log and a log file from each node in the cluster. This is extremely tedious to parse through. Thus, we utilize an AI agent that is designed to take all the artifacts from a fuzzer run and produce a structured summary. It reads the scenario to understand what the test was trying to do, reads the validation results to know which invariants were violated, reads the node logs and correlates timestamps across nodes to reconstruct the sequence of events, and produces a short report: what the test did, what failed, the most likely root cause, and pointers to the specific log lines that support the hypothesis. The agent is a first-pass triage layer, not a replacement for an engineer. Its job is to compress ten thousand log lines into one paragraph that tells you where to look.

Example: https://github.com/valkey-io/valkey-fuzzer/issues/93


6. Call to action (1 Minute)

Some asks for the audience. 

Try it out by following these steps. First https://github.com/valkey-io/valkey-fuzzer#installation then https://github.com/valkey-io/valkey-fuzzer#usage. If they run the fuzzer and find any bugs they can open issues on the GitHub: https://github.com/valkey-io/valkey-fuzzer/issues

Contribute: the fuzzer itself is open source, and concrete areas to help include more operations (today we only support failover, and we want resharding, add-replica, remove-replica, scale-out, scale-in), more chaos (today we do process kills, and we want network partitions, packet drops, etc). We also want to include mixed-version cluster testing for rolling upgrades. 


Links to put on the closing slide: Valkey at https://valkey.io, the 1 billion RPS blog at https://valkey.io/blog/1-billion-rps/, the fuzzer repo at https://github.com/valkey-io/valkey-fuzzer, and the daily fuzzer runs at https://github.com/valkey-io/valkey-fuzzer/actions/workflows/fuzzer-run.yml.

7. Q&A (2 Minutes)