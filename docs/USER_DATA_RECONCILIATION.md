# Demo user-data reconciliation

## Change card — 2026-09-10

- Type: operational fix, with a runbook correction.
- Baseline: `3b8b2c3`, image `m7.3`, AWS provider `6.63.0`.
- Problem: a refreshed, untargeted demo plan proposes one instance replacement solely
  because `user_data_base64` differs. The stopped instance's root volume is deleted on
  termination and holds PostgreSQL data.
- Scope: reconcile the existing stopped instance's launch metadata with the repository;
  preserve the instance, volume, image, application, model and Terraform lifecycle rules.
- Expected result: a refreshed full demo plan with no infrastructure changes. The
  instance ID, root volume ID and stopped state must remain unchanged.
- Method: retain old user data for rollback, validate the exact planned payload, update
  only the EC2 user-data attribute, then verify it by reading EC2 and planning again.
- Runtime boundary: updating user data does not install or execute the embedded scripts.
  The earlier manual installation is documented in `OPERATIONS.md`; the disk has not
  been inspected in this repair. No runtime or database-content verification is claimed.
- Evaluation: no model calls or benchmark rerun; neither agent behavior nor images change.

## Baseline evidence

A live EC2 read matched the remote Terraform state's user data. Eight embedded files
matched the repository; only `restore.sh` differed: three explanatory comment lines and
`>/dev/null` on the `psql` command. The AMI was unchanged. Using the deployed parameters
and image `m7.3`, the full plan returned exit code 2 and
`Plan: 1 to add, 0 to change, 1 to destroy`, with replacement path
`["user_data_base64"]`.

All 64 deployment-file tests passed before the repair. Pytest also reported a local
cache-write permission warning; it did not fail the tests.

## Verification

Completed on 2026-09-10, against the existing `m7.3` deployment:

1. A fresh full plan reproduced the single `user_data_base64` replacement.
2. The decoded planned cloud-init payload differed only in the embedded `restore.sh`.
   Removing comments and blank lines left exactly the known `>/dev/null` correction.
   The payload was also within EC2's 16 KiB decoded limit.
3. The old and desired compressed payloads were saved locally before mutation. Immediately
   before the write, the instance was still stopped and its user data still matched the
   saved old value.
4. Only the EC2 `userData` attribute was updated using `modify-instance-attribute`.
   A read-back matched the desired payload exactly. No instance start, stop, termination,
   volume operation or application command was issued.
5. The first full post-update plan had zero resource changes but exit code 2, reflecting
   the external update relative to stored state. A reviewed refresh-only plan then saved
   the observed state without changing infrastructure.
6. The final refreshed full plan returned exit code **0**, with zero resource changes and
   no `resource_drift`. The instance ID, root volume ID, AMI and stopped state matched
   their pre-update values. Terraform configuration and provider versions were unchanged.

Post-change local checks: all 64 deployment-file tests passed, `terraform validate`
passed, and `git diff --check` passed. Only documentation is changed in Git; the live
metadata repair does not require a new application image. Remote CI is checked on the
documentation PR before merge.

Payload SHA256 values (compressed bytes, before base64 encoding):

| Payload | SHA256 |
|---|---|
| Before | `87f06b18405523c7d373d3437692fcf251f4be799eab04613792e127edc7f560` |
| Reconciled | `079ace91fe4be9a4ca2063f6db60e0a1723eba89127642f2c1ad4f3b46b6d4a3` |

The rollback payload and local evidence remain under the ignored directory
`deploy/terraform/demo/.terraform/user-data-reconciliation/`. The temporary plan files
were removed. These local files are not part of the public report or a portable backup.
The original payload can be restored with AWS's documented stopped-instance user-data
update procedure; doing so reinstates the known Terraform replacement difference.

**PASS for launch-metadata reconciliation.** This does not demonstrate login, chat,
restore execution, on-disk script contents or database contents. Those require a separate
runtime check when the demo is next started. The existing-instance script must be checked
before claiming that the earlier manual runtime correction has been independently verified.

## References

- [AWS: updating a stopped instance's user data](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html#user-data-view-change)
- [Terraform: plan refresh and speculative plans](https://developer.hashicorp.com/terraform/cli/commands/plan)

This is a one-time metadata reconciliation, not a general mechanism for delivering
machine changes. A later change to installed scripts or units still needs an explicit
installation or a planned replacement with archive and restore verification.
