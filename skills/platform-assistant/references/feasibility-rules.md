# Runtime feasibility rules

Use this reference for every OrganizationSpec proposal, organization version change, Task submission, and Runtime start. The product validator, not the assistant's prose, is the final enforcement point.

## Gate outcomes

- **Feasible**: The selected binding has a current capability profile that satisfies all declared hard requirements and policy constraints. The next product transition may proceed after its normal confirmation rule.
- **Conditional**: The request could become feasible after a declared constraint, resource, input limit, or binding change is resolved. Keep it in preview or draft state; do not confirm, publish, submit, or start it.
- **Blocked**: The profile contradicts a hard requirement or a product policy. Do not offer a confirmation override.
- **Capability unknown**: The profile does not declare enough evidence for the requested workload. Fail closed for required, resource-intensive, GUI, hardware, proprietary-software, or platform-specific work.

The product may expose these outcomes as API fields or error codes, but their meaning must remain stable. A user confirmation is not evidence that an unknown or blocked workload is executable.

## Capability profile

`RuntimeCapabilityProfile` is product-owned and versioned. A `RuntimeBinding` selects a profile; the assistant must read the selected profile instead of inferring capabilities from a provider name, model name, or Thread history.

The profile must be able to declare:

- Operating-system family, version, and architecture, such as Linux or Windows.
- Headless or GUI availability and any display-session requirement.
- CPU and memory capacity class, concurrency, maximum duration, and workload limits.
- GPU presence, type, memory, and supported accelerator runtimes.
- Installed tools and their versions, language runtimes, package dependencies, and licenses.
- Network and external-service policy, including reachable domains where relevant.
- Attached hardware or proprietary applications.
- Supported input and output media, size limits, and persistence locations.
- Profile revision, observation time, source, and whether the declaration is trusted.

Unknown fields remain unknown. Do not silently substitute the development host's capabilities for a production Runtime profile.

## Workload requirements

Normalize each role responsibility and each user Task into a structured requirement set before validation. Include only requirements supported by the request, existing product policy, or a declared role contract. Requirements may cover:

- OS or architecture constraints.
- GUI, display, or interactive-session requirements.
- Minimum CPU, memory, GPU, accelerator, duration, concurrency, or input-size class.
- Required tools, runtimes, packages, services, hardware, or network access.
- Input and output media and storage constraints.

When natural language leaves a required capability ambiguous, mark it conditional or unknown and ask the user to narrow the workload or select a suitable binding. Do not assume that a general-purpose Codex role can perform every operation mentioned in its responsibility.

## Hard policy examples

- A Windows-only operation, such as a Windows GUI, COM, registry, or PowerShell-specific workflow, cannot run on a Linux binding. Block it and suggest a Linux-native implementation or a Windows-capable binding.
- A GUI-required operation cannot run on a headless Runtime. Block it and suggest a headless tool or an interactive workstation.
- GPU-required work cannot run when the profile declares no suitable GPU. If GPU capacity is not declared, return capability unknown and block the start.
- Video editing, large media transcoding, 3D rendering, model training, or other CPU/GPU-intensive work requires an explicit suitable capacity profile and declared limits. A normal Codex binding is not proof of those resources.
- Proprietary software or attached hardware must be explicitly present in the selected profile. Do not assume that an installer, license, camera, device, or desktop session exists.
- A required tool, package, network route, or media format missing from the profile is a hard mismatch. An unknown declaration is not support.

For a blocked or unknown result, give the reason, the affected role or Task, the profile evidence, and a concrete alternative. Alternatives can include a Linux/headless tool, a smaller workload, an external rendering or conversion service, a different Runtime binding, or manual handling outside the organization.

## Enforcement points

Run the same validator at all of these boundaries:

1. When creating a proposal, to show findings in preview.
2. Before confirmation and publication, to prevent an infeasible OrganizationSpec from becoming active.
3. Before Task submission, against the current published spec and current bindings.
4. Immediately before Runtime start, to catch profile drift or revoked tools/resources.

Persist the check identity, profile revision, requirement summary, outcome, findings, and validator version with the proposal or Task. Reuse a check only while its input hashes and profile revisions still match. Never let a prompt-only decision or a stale check bypass the product validator.
