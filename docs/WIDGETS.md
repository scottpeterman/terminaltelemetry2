# Widget design guide

A widget is one YAML file. It names a command per platform, a TextFSM template
that parses the output, a mapping from template fields to stable names, and a
view. The schema is small; what makes a widget work across four vendors is a
handful of patterns that aren't visible from the schema alone. This guide covers
those patterns using the bundled widgets as worked examples.

Widgets load from `terminaltelemetry2/data/widgets/` and then
`~/.terminaltelemetry2/widgets/`. A user file with the same `widget:` name
replaces the bundled one, so the fastest way to change a bundled widget is to
copy it there and edit it. A definition error is reported at load time with the
file and key, and that widget is skipped; the rest still load.


## 1. Anatomy

```
command (per platform) -> template -> records -> field aliases -> drop -> unique
                       -> rates / deltas -> computes -> history -> view
```

That order matters. `drop` sees only aliased fields (not rates or computes);
computes see fields, rates, deltas and earlier computes; views see everything.

The smallest useful widget:

```yaml
widget: version
title: Version
interval: 300
commands:
  arista_eos: show version
  cisco_ios: show version
  cisco_nxos: show version
  juniper_junos: show version
fields:
  hostname: [HOSTNAME]
  model: [MODEL, PLATFORM, HARDWARE]
  version: [SOFTWARE_VERSION, VERSION, OS, JUNOS_VERSION]
  serial: [SERIAL_NUMBER, SERIAL]
view:
  type: kv
  show: [hostname, model, version, serial]
```

### Top-level keys

| Key | Required | Meaning |
|---|---|---|
| `widget` | yes | Unique name; user files override bundled ones by this name. |
| `title` | no | Tile caption. Defaults to `widget`. |
| `interval` | no | Poll seconds, minimum 5, default 30. |
| `commands` | yes | `platform: command`. Platforms: `arista_eos`, `cisco_ios`, `cisco_nxos`, `juniper_junos`. |
| `templates` | no | `platform: template name`. Omitted platforms resolve automatically (section 2). |
| `fields` | yes | `name: [TEMPLATE_VALUE, ...]`. First non-empty alias wins. |
| `key` | for rates, deltas, spark, `unique` | Field that identifies a row across polls. |
| `rates` | no | `name: source_field`. Per-second rate of a counter, per key. |
| `deltas` | no | `name: source_field`. Change since the last poll, per key. |
| `computes` | no | `name: "expression"`. Arithmetic over fields (section 7). |
| `drop` | no | Rules; a record matching any rule is discarded (section 5). |
| `unique` | no | `true` collapses rows sharing `key`, first wins (section 6). |
| `monitor` | no | Table column holding an interface name; adds right-click Monitor Tx/Rx. |
| `view` | yes | `table`, `kv` or `stat` (section 8). |


## 2. How a template is chosen

For each platform the parser resolves a template in this order, first hit wins:

1. **Explicit.** The widget names one under `templates:`. Only that template's
   family is tried: the name itself, then numbered siblings `<name>2`, `<name>3`
   ... newest first, from override files and the DB. Nothing outside the family
   is ever used.
2. **Remembered.** The template that last worked for this platform, command and
   request (explicit name or automatic).
3. **Exact.** `<platform>_<command>` with spaces and hyphens as underscores:
   `arista_eos` + `show ip bgp summary` -> `arista_eos_show_ip_bgp_summary`.
4. **Scored sweep.** Every template whose name contains all of the command's
   terms longer than two characters, scored against the output; best wins.

A template that returns zero records is forgotten and resolution restarts on
that poll.

**Name a template explicitly when:**

- The command has a pipe. `show system processes extensive | no-more` would
  derive an exact name containing `|_no_more`, which matches nothing. `system`
  and `top_procs` name `juniper_junos_show_system_processes_extensive`.
- Two widgets parse the same command differently. IP Addresses names
  `arista_eos_ifaddr` for `show interfaces`, which Interface Up/Down resolves
  automatically. Remembered templates are kept per request, so each widget
  keeps its own (this was a real bug once; see pitfall 9).
- The sweep can pick a wrong-but-scoring template. A failed EOS BGP summary
  parse once scored `show_ip_interface_brief` higher than nothing. A wrong table
  is worse than an error.

Otherwise leave it automatic: the exact name is fast, and the sweep covers
releases the exact template doesn't.

### Template content: override files, then the DB

A template name resolves to text from `~/.terminaltelemetry2/templates/<name>.textfsm`,
then the bundled `data/templates/<name>.textfsm`, then the DB
(`~/.terminaltelemetry2/tfsm_templates.db`, seeded from the bundled copy on first
run). An override file shadows the DB row of the same name, including for every
other widget and device using it. The Template Lab shows where each lives:
`[override]` or `[db:<source>]`.

### Naming custom templates

The sweep includes every template whose name contains all the command's terms.
A custom template for a second view of a shared command must drop at least one
term so the sweep can never hand it to the automatic widget:

```
arista_eos_show_interfaces         # ntc, Interface Up/Down (automatic)
arista_eos_ifaddr                  # custom, IP Addresses (explicit) - no "show"/"interfaces"
```

Don't end a base template name in digits you don't mean as a sibling suffix.
Families match the exact prefix (`..._vpnv4` + `2` = `..._vpnv42`), but a human
reading `..._extensive5` will assume it's the fifth sibling.


## 3. Sharing polls

The broker polls each distinct command string once, at the shortest interval
any subscriber asked for. Widgets that use the same command share the poll;
widgets that also use the same template share the parse.

| Command (EOS) | Widgets | Polls | Parses |
|---|---|---|---|
| `show interfaces` | Interface Up/Down (automatic), IP Addresses (`arista_eos_ifaddr`) | 1 | 2 |
| `show processes top once` | System, Top Processes | 1 | 1 |
| `show ip bgp summary` | BGP, BGP Peers | 1 | 1 |

So the command string is the unit of cost. Match it exactly (`show interface`
on NX-OS, `show interfaces` elsewhere) to reuse an existing poll. A new widget
that needs different fields from output the window already collects is free on
the device: write a second template, name it explicitly, and use the same
command.


## 4. Aliases are cross-platform fallbacks

Each field lists template value names in priority order; the first non-empty
one wins, matched case-insensitively. That's how one widget spans vendors whose
templates name the same thing differently:

```yaml
peer:  [BGP_NEIGH, BGP_NEIGHBOR, PEER_IP, NEIGHBOR]
state: [STATE, STATE_PFXRCD, STATE_OR_PREFIXES_RECEIVED]
```

The same mechanism gives a within-platform fallback when a value is sometimes
missing. `system.yaml` takes CPU idle from the `CPU:` summary line when the
output has one, and otherwise from the `idle` process's WCPU:

```yaml
cpu_idle: [GLOBAL_CPU_PERCENT_IDLE, PERCENT_CPU]
```

That only works because a `drop` rule (next section) guarantees the surviving
row, on outputs with no summary line, is the idle process. Fallback aliases and
record selection usually come as a pair.

TextFSM `List` values arrive joined with `, `.


## 5. `drop` selects records

`drop` removes records before anything else sees them. It's usually described
as filtering, but its more useful job is choosing which record a `kv` or `stat`
view is built from.

A rule is a single condition, or `when:` with several conditions that must all
hold. Any matching rule drops the record.

```yaml
drop:
  - {field: command, op: match, value: "^idle"}           # single condition
  - when:                                                 # AND
      - {field: cpu_line, op: empty}
      - {field: proc, op: not_empty}
      - {field: proc, op: not_match, value: "^idle$"}
```

Operators: `eq ne lt le gt ge` (numeric when both sides parse, else string for
`eq`/`ne`), `match not_match` (Python `re.search`), `numeric not_numeric`,
`empty not_empty`.

**Scope the rule to the case it's for.** The `system.yaml` rule above only
fires when there's no CPU line *and* there is a process column, so EOS and
Junos 23.x (CPU line) and IOS/NX-OS (no process column) pass through untouched.
An unscoped "keep only idle" would have emptied the tile on every platform
without an idle process.

**Helper fields.** Conditions can only test aliased fields, so a rule may need a
field that isn't displayed (`cpu_line`, `proc` above). In a `kv` view, list the
displayed fields in `show:` so helpers stay hidden; in a table, leave them out
of `columns:`.

**Drop noise by value, as narrowly as the value allows.** From
`ip_addresses.yaml`:

```yaml
- {field: address, op: match, value: "(?i)^(fe80:|fec0:|127\\.|128\\.0\\.0\\.)"}
- {field: address, op: match, value: "^10\\.0\\.0\\.\\d+/8$"}      # exact /8 only
```

Junos's internal RE network is `10.0.0.x/8`; matching the mask as well means a
real `10.0.0.1/30` on a customer link still shows. Put inline regex flags such
as `(?i)` at the very start of the pattern.


## 6. `unique`

Some templates emit one record per sub-line. Junos `show interfaces terse`
produces a record per address family and per address, so `ae0.9` with inet,
inet6 and mpls appears three or more times. `unique: true` keeps the first
record per `key`, which is the interface line itself:

```yaml
key: port
unique: true
```

It's opt-in because some keys legitimately repeat. Two LLDP neighbors can share
a local port on a management segment, parallel OSPF adjacencies share a
neighbor ID, and Junos threads share a PID. Turning it on there would hide real
rows. Use it only where repetition is an artifact of the template.


## 7. Rates, deltas and computes

`rates` and `deltas` difference a counter across polls, per `key`:

```yaml
key: intf
rates:
  in_pps: in_pkts          # per second
deltas:
  flaps: changes           # since the last poll
```

A counter that goes backwards (cleared, wrapped, rebooted) yields a blank for
one sample rather than a negative number. History resets when the session
drops.

`computes` are arithmetic over fields, rates, deltas and earlier computes:
`+ - * /`, unary minus, parentheses and numeric literals. No functions,
comparisons or string handling. An operand with a trailing unit (`1000000 Kbit`,
`1500 bytes`) uses its leading number. A missing or non-numeric operand, or
division by zero, makes the result blank rather than an error.

```yaml
computes:
  in_util: "in_rate / (bw_kbit * 1000) * 100"
  cpu_busy: "100 - cpu_idle"
```

Because computes can't branch or join strings, pick the source with aliases
and `drop` (sections 4 and 5), then compute. An IPv6 address and its prefix
printed separately stay two columns (`address`, `subnet`).

`drop` runs before computes, so a rule can't test a computed value.


## 8. Views

### `kv`

Label/value pairs from the **first** row. Empty values hide their line, so one
definition can span platforms whose templates expose different fields. The
System tile shows CPU user/sys on EOS and 5s/1m/5m on IOS from the same YAML.

The default `show` list is fields, rates and deltas, **not computes**. A kv
widget with computes needs an explicit `show:`, which also keeps helper fields
off the tile.

### `table`

```yaml
view:
  type: table
  columns: [intf, family, address, subnet, status]
  labels: {intf: Interface, family: AF}
  sort: intf
  sort_desc: false
  limit: 12                # top-N; requires sort
  alerts:
    - {field: status, op: not_match, value: "(?i)^up", style: muted}
  cells:
    family: {type: status, rules: [...]}
```

Row `alerts` use the first matching rule: `alert`, `warn` and `ok` tint the
background, `muted` greys the text.

Cell renderers:

| Type | Options | Notes |
|---|---|---|
| `bar` | `min` (0), `max` (100), `suffix`, `thresholds: [{at, style}]` | Last threshold reached sets the color. |
| `status` | `rules: [{..., style, text}]` | Dot plus text. First matching rule wins; `text` replaces the value. |
| `spark` | `history` (30), `min`, `max` | Needs `key`. Blank samples are skipped, not drawn as zero. |

Status rules can test any field in the row, not just their own column. The AF
dot in IP Addresses follows the Link column:

```yaml
family:
  type: status
  rules:
    - when: [{field: family, op: match, value: "(?i)^(inet6|ipv6)$"},
             {field: status, op: not_match, value: "(?i)^up"}]
      style: alert
      text: IPv6
    - {field: family, op: match, value: "(?i)^(inet6|ipv6)$", style: ok, text: IPv6}
```

Put the more specific rule first.

`monitor: <column>` adds right-click **Monitor <intf> -> Tx / Rx / Tx / Rx**.
The column must be displayed. The traffic monitor issues its own per-interface
counter command, so the table's template doesn't need counters.

### `stat`

A single number from `count`, `sum`, `min` or `max` over the rows matching
`where`:

```yaml
view:
  type: stat
  aggregate: count
  where:
    when:
      - {field: state, op: not_numeric}
      - {field: state, op: not_match, value: "^Estab"}
  label: peers not established
  alert_above: 0
```

`sum`, `min` and `max` also take `field:`.


## 9. Writing the template

The widget can only be as good as its template, and most template work happens
in the Template Lab against a real capture. Some patterns that recur:

**Anchor rows from the stable side.** Junos top's STATE column is six characters
wide and can hold spaces (`long p`, `PCI Sc`) or `-`. The process template
matches STATE lazily and anchors on `TIME` and `WCPU%` to the right, so the
columns either side of it always land correctly.

**Let the header choose the state.** Three Junos releases print three process
table layouts (a `C` column, a `THR` column, neither). One template handles all
three by sending each header to its own state:

```
^\s*PID\s+USERNAME\s+PRI\s+NICE\s+SIZE\s+RES\s+STATE\s+C\s+TIME ... -> Process_C
^\s*PID\s+USERNAME\s+THR\s+PRI ...                                 -> Process_THR
^\s*PID\s+USERNAME\s+PRI\s+NICE\s+SIZE\s+RES\s+STATE\s+TIME ...    -> Process_Plain
```

One template spanning releases beats a sibling per release.

**One record per repeated thing, with Filldown for the parent.** The `*_ifaddr`
templates fill down `INTERFACE`, `LINK_STATUS` and `FAMILY`, and record on each
address line. The interface line clears everything first:

```
^\S+\s+is\s+ -> Continue.Clearall
^${INTERFACE}\s+is\s+${LINK_STATUS}\s*(?:,.*)?$$
^\s+${FAMILY}\s+[Aa]ddress\s+is\s+${ADDRESS}(?:\s.*)?$$ -> Record
```

**End with an empty `EOF` state** when values fill down. Otherwise TextFSM
records an extra row at end of input carrying only the filled-down values.

**No `Error` rule in an explicitly named template.** It records what it matches
and ignores the rest (trailing prompt, `{master:0}`, absent lines). An `Error`
turns one unexpected line into a dead tile.

**Summary lines use Filldown** so every table row carries them, and a `kv` view
reading row 0 sees load, memory and task counts wherever row 0 comes from.

### Lab workflow

1. A failing widget tints its `{ }` button red. Click it: the lab opens with the
   exact capture and the template the widget tried, and runs Test.
2. The **Template** dropdown lists the base and its numbered siblings, where
   each lives, and which one the widget is using (`● in use`).
3. Edit and **Test** until the result table is right. A state error jumps to the
   input line and the template rule.
4. Save:
   - **Save as override** writes `templates/<selected name>.textfsm`. It shadows
     the DB copy for every widget and device. Use it when the fix is a strict
     improvement (the three-layout Junos template).
   - **Save to database** writes a sibling. With the base selected it creates
     the next number; with a sibling selected it updates that sibling in place.
     The base keeps working for gear it already parses, and the sibling catches
     the rest.
5. **Delete sibling** removes failed attempts. A stale sibling that happens to
   return records for some other release can win over a newer one.

Commit a real capture to `tests/fixtures/` and a test for any template you'll
rely on. Every template fix in this repo landed with the capture that broke it.


## 10. Worked examples

### `system.yaml`: one tile, four vendors, three Junos layouts

```yaml
widget: system
interval: 20
commands:
  arista_eos: show processes top once
  cisco_ios: show processes cpu
  cisco_nxos: show processes cpu
  juniper_junos: show system processes extensive | no-more
templates:
  juniper_junos: juniper_junos_show_system_processes_extensive   # pipe -> explicit
fields:
  cpu_user: [GLOBAL_CPU_PERCENT_USER]
  cpu_idle: [GLOBAL_CPU_PERCENT_IDLE, PERCENT_CPU]               # summary, else idle proc
  cpu_5s:   [CPU_5_SEC]                                          # IOS / NX-OS
  mem_used: [GLOBAL_MEM_USED]
  cpu_line: [GLOBAL_CPU_PERCENT_IDLE]                            # helper
  proc:     [COMMAND]                                            # helper
drop:
  - when:                                                        # no CPU line + process table
      - {field: cpu_line, op: empty}
      - {field: proc, op: not_empty}
      - {field: proc, op: not_match, value: "^idle$"}
computes:
  cpu_busy: "100 - cpu_idle"
view:
  type: kv
  show: [cpu_busy, cpu_user, cpu_idle, cpu_5s, mem_used]         # computes need show:
```

What each platform ends up showing:

| Output | Row the tile reads | CPU idle from | CPU busy |
|---|---|---|---|
| EOS, Junos 23.x | first process (all kept) | `CPU:` line | computed |
| Junos 13.x, QFX5100 | the idle process (rest dropped) | idle WCPU | computed |
| IOS, NX-OS | the only record | none (hidden) | blank (hidden); 5s/1m/5m shown |

`top_procs` shares the command and the template, so both tiles cost one poll
and one parse. Its own `drop` removes the idle rows from the top-N, and doesn't
affect `system`.

### `ip_addresses.yaml`: a second view of polls already made

```yaml
commands:                               # identical strings to Interface Up/Down and Ports
  arista_eos: show interfaces
  cisco_ios: show interfaces
  cisco_nxos: show interface
  juniper_junos: show interfaces terse
templates:                              # named to stay out of the sweep
  arista_eos: arista_eos_ifaddr
  cisco_ios: cisco_ios_ifaddr
  cisco_nxos: cisco_nxos_ifaddr         # Junos: automatic, same template as Ports
fields:
  family:  [FAMILY, PROTO]              # Internet/IPv6 vs inet/inet6
  address: [ADDRESS, LOCAL]
  status:  [LINK_STATUS, LINK_STATE]
drop:
  - {field: family, op: not_match, value: "(?i)^(inet6?|internet|ipv6)$"}
  - {field: address, op: empty}
  - {field: intf, op: match, value: "^(pfe|pfh|lc|jsrv)-|\\.(1638[3-5]|3276[7-9])$"}
```

The widget adds no device commands on any platform. The two vocabularies for
address family (`Internet`/`IPv6` from `show interfaces`, `inet`/`inet6` from
Junos) are normalized at display time by the status cell's `text:`, not in the
data.

### `port_status.yaml`: a template artifact fixed in the widget

```yaml
commands:
  juniper_junos: show interfaces terse
key: port
unique: true            # terse repeats a unit per family/address
monitor: port
```

The terse template is right to emit a record per address, and IP Addresses
depends on it. The repetition is only wrong for a one-row-per-port view, so
the fix belongs in this widget, not the template.


## 11. Pitfalls

1. A command with a pipe needs an explicit template.
2. A kv view with computes needs `show:`, or the computed values never appear.
3. `drop` can't see computes, rates or deltas. Add a helper field instead.
4. An unscoped `drop` that selects "the right row" on one platform can empty the
   tile on another. Condition it on something only that platform's output has.
5. `rates`, `deltas`, spark cells and `unique` need `key`; the loader says so.
6. Don't use `unique` where keys can legitimately repeat (LLDP, OSPF, processes).
7. A custom template for a shared command must leave a command term out of its
   name, or the sweep can pick it for the automatic widget.
8. An override file shadows the DB copy for every widget and device. Prefer a
   sibling unless the fix is a strict improvement.
9. Two widgets sharing a command with different templates must each resolve
   theirs. Explicit names are always safe; the parser remembers templates per
   request, so an explicit and an automatic widget on one command don't collide.
10. An empty `stat` tile on a box that doesn't run the protocol reads as an
    error, not zero. It's cosmetic; leave the widget out of that box's layout if
    it bothers you.
11. A widget with no command for a platform shows "no command defined". Add the
    command, or leave the widget out of that platform's layout.
12. Filldown values plus no `EOF` state produce a phantom last row.
13. Inline regex flags (`(?i)`) go at the start of the pattern.


## Layouts

Widgets appear only if a layout places them. Layouts live in
`data/layouts/` and `~/.terminaltelemetry2/layouts/` (user files override by
`layout:` name). `--layout NAME` forces one; otherwise the first layout
listing the device's platform wins, else `default`.

```yaml
layout: eos
platforms: [arista_eos]
terminal: {position: left, size: 0.4}
rows:
  - [version, system, bgp_down, ospf_down, port_errdisabled]
  - [bgp_peers, ospf_neighbors]
  - [lldp_neighbors, top_procs]
  - [{name: intf_updown, span: 3}, {name: ip_addresses, span: 2}]
```

`span` is relative width within the row. A new widget needs a layout entry, and
a user copy of a layout does not pick up entries added to the bundled one.