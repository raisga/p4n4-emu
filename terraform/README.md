# Terraform for cloud emulators

Terraform for the cloud emulators in the guides [Emulators on AWS](https://github.com/raisga/p4n4-docs/blob/main/guides/aws.md), [Google Cloud](https://github.com/raisga/p4n4-docs/blob/main/guides/gcp.md) and [Azure](https://github.com/raisga/p4n4-docs/blob/main/guides/azure.md). Each cloud has two root modules:

| Module | Applied | Creates | Guide |
|---|---|---|---|
| `<cloud>/shared` | Once per account, by an admin | The bucket or storage account for reports and Terraform state, the instance role or reaper role, the network or firewall, the budget | Section 3 (and the budget from 2.x) |
| `<cloud>/emulator` | Once per run, by anyone on the team | One emulator: the VM, its disk and, on Azure, its own resource group, network and identity | Section 4 |

```
terraform/
├── aws/     shared/  emulator/   EC2 Graviton, SSM, S3; the instance terminates itself at its TTL
├── gcp/     shared/  emulator/   C4A Axion, IAP, GCS; Google Cloud deletes the VM at max-run-duration
└── azure/   shared/  emulator/   Cobalt 100, a resource group per run; the VM deletes its group at its TTL
```

Everything after the VM is up stays as in the guides: running the project (section 5), the report (section 6) and the cost report (6.3). The emulator module prints the commands for each run as outputs.

**Still manual**, because Terraform can't do it or shouldn't: section 1, and in section 2 the sign-up, MFA, users and their roles:

| Cloud | Manual steps |
|---|---|
| AWS | Account and root MFA (2.1–2.3), IAM Identity Center and the `P4n4Emulator` permission set (2.4), CLI login (2.5), quotas (2.6) |
| Google Cloud | Account and paid upgrade (2.1–2.2), creating the project and linking billing (2.4), turning on the billing export in the console (2.5), teammates' roles (2.6), quotas (2.7) |
| Azure | Account and pay-as-you-go upgrade (2.1–2.2), CLI login (2.3), teammates' roles with the RBAC Administrator condition (2.5; `terraform output reaper_role_id` gives the role ID), quotas (2.6) |

## Requirements

- Terraform ≥ 1.10 (S3's native state locking needs it)
- The cloud's CLI, signed in, as in the guide's section 2: `aws sso login`, `gcloud auth application-default login`, `az login`. Terraform uses those credentials

## Set up once (an admin)

```bash
cd tools/emu/terraform/aws/shared                    # or gcp/shared, azure/shared
terraform init
terraform apply -var 'budget_emails=["aws@example.com"]'
#   gcp:   terraform apply -var project=p4n4-emu-4f2a9c -var billing_account=0X0X0X-0X0X0X-0X0X0X
#   azure: terraform apply -var subscription_id=<id> -var 'budget_emails=["azure@example.com"]'
terraform output -raw backend_hcl > ../emulator/backend.hcl
```

`backend.hcl` tells the emulator module where its state lives: `tfstate/` in the bucket (AWS, Google Cloud), or the `tfstate` container (Azure). It names a bucket, not a secret, and every teammate needs the same file. Give it to them, or have them write it from `terraform output` themselves. It's in `.gitignore`.

**The shared module's own state** stays in `terraform.tfstate` next to it, unless you move it into the same bucket. Move it if another admin may change the shared setup:

```bash
cat > backend_override.tf <<'EOF'
terraform {
  backend "s3" {}                                     # gcs on Google Cloud, azurerm on Azure
}
EOF
terraform init -migrate-state -backend-config=../emulator/backend.hcl \
  -backend-config=key=shared.tfstate                  # gcp: -backend-config=prefix=tfstate/shared
rm terraform.tfstate terraform.tfstate.backup
```

`backend_override.tf` is in `.gitignore`. Keep it, because every later `terraform init` there needs it.

**On AWS, after the first run,** activate the cost allocation tags. AWS only lists a tag key once a resource has carried it:

```bash
terraform apply -var activate_cost_tags=true
```

**Already followed the guide's CLI steps?** Those resources have the same names, so `apply` fails with "already exists". On AWS and Google Cloud, import them:

```bash
# AWS (aws/shared); add -var budget_emails=… if you made the budget
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
terraform import aws_s3_bucket.emu p4n4-emu-$ACCOUNT
terraform import aws_s3_bucket_public_access_block.emu p4n4-emu-$ACCOUNT
terraform import aws_iam_role.instance p4n4-emu-instance
terraform import aws_iam_role_policy_attachment.ssm p4n4-emu-instance/arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
terraform import aws_iam_role_policy.bucket p4n4-emu-instance:bucket
terraform import aws_iam_instance_profile.instance p4n4-emu-instance
terraform import aws_security_group.emu $(aws ec2 describe-security-groups --group-names p4n4-emu --query 'SecurityGroups[0].GroupId' --output text)
terraform import 'aws_budgets_budget.monthly[0]' $ACCOUNT:p4n4-monthly

# Google Cloud (gcp/shared), with -var project=$PROJECT on each
terraform import google_compute_network.emu projects/$PROJECT/global/networks/p4n4-emu
terraform import google_compute_firewall.iap_ssh projects/$PROJECT/global/firewalls/p4n4-emu-iap-ssh
terraform import google_storage_bucket.emu $PROJECT
terraform import google_bigquery_dataset.billing_export projects/$PROJECT/datasets/billing_export
```

On Azure, the storage account's name is random, so the guide's account can't be imported as is. Delete the guide's `p4n4-emu-shared` resource group and the `p4n4 Emulator Reaper` role first, while no run uses them, and let Terraform create both.

## Each run (anyone on the team)

The **workspace is the run**. Its name starts with the profile, which is how the module knows what to build. That's why `apply` and `destroy` need no variables, and why a teammate can destroy your run by selecting its workspace.

```bash
cd tools/emu/terraform/aws/emulator                  # or gcp/emulator, azure/emulator
terraform init -backend-config=backend.hcl           # once per checkout

terraform workspace new rpi5-$(date +%Y%m%d-%H%M)    # <profile>-<anything>: rpi5-…, nuc-…, rpi-zero2w-…
terraform apply                                      # shows the plan; type yes
terraform output                                     # connect, grafana, report, ttl_expires
```

Then follow the guide from section 5: connect, put the project on the VM, `p4n4-emu up`, watch it, and take the report with the command in `terraform output report`. When you're done:

```bash
terraform destroy
terraform workspace select default && terraform workspace delete rpi5-20261015-1430
```

**If the TTL got there first**, because the instance terminated itself or Google Cloud or the VM deleted the run, `terraform destroy` finds nothing left and just empties the state. Delete the workspace as above.

**Everyone's runs:** `terraform workspace list`, and the cloud-side checks in the guide's section 7.2 to catch runs whose state was lost.

### Options

Set them with `-var name=value` on `apply`. `terraform destroy` needs none of them.

| Variable | AWS | Google Cloud | Azure | Default |
|---|---|---|---|---|
| Profile, if the workspace name doesn't start with one | `profile` | `profile` | `profile` | From the workspace |
| Machine size | `instance_type` | `machine_type` (+ `boot_disk_type`) | `size` | The profile's (guide 4.1) |
| Disk | `root_volume_gb` | `boot_disk_gb` | `os_disk_gb` | 40 / 40 / the profile's |
| TTL | `ttl_minutes` | `ttl_minutes` | `ttl_minutes` | 480 (8 h) |
| Where | `region` | `zone`, `project` | `location`, `subscription_id` | CLI default / `us-central1-a` / `eastus` |
| p4n4 versions on the VM | `git_refs = { emu = "v0.2.0" }` | the same | the same | `main` for all three repos |
| SSH | | | `ssh_public_key_path`, `ssh_source_cidr` | `~/.ssh/id_ed25519.pub`, your IPv4 address |

A cheaper smoke test: `terraform apply -var instance_type=t4g.xlarge` on AWS, `-var machine_type=e2-standard-4 -var boot_disk_type=pd-balanced` on Google Cloud with an `nuc-…` workspace (E2 is x86), or `-var size=Standard_B4ps_v2` on Azure.

**GPU profile (`jetson-orin-nano`).** On AWS the module boots the Deep Learning AMI, which has the NVIDIA driver, the Container Toolkit and Docker. On Google Cloud and Azure, install the driver and toolkit on the VM as in the guide's section 4.4.

## Tests

The emulator modules have `terraform test` suites that plan against mock providers. They need no cloud account. They check the profile table, the overrides, the TTL and the GPU and `nuc` variants, and that an unknown profile or a bad workspace name fails:

```bash
cd tools/emu/terraform/aws/emulator
terraform init -backend=false
TF_WORKSPACE=rpi5-20261015-1430 terraform test
```

CI (`.github/workflows/terraform.yml`) runs `fmt`, `validate` and these tests for every module.
