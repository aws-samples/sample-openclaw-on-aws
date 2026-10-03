# Capacity, persistence, and cost controls

Review these controls together. Node supply, application replicas, persistent
storage, and spend limits solve different problems.

## EKS Auto Mode behaviour

The custom NodePools are demand-driven. Pending Pods that match a NodePool can
cause Auto Mode to provision a node. Auto Mode can later consolidate,
replace, or expire nodes according to the NodePool disruption settings.

Auto Mode does not change application replica counts. This sample configures:

- One OCE API replica.
- One OCE worker replica.
- One PostgreSQL replica.
- One gateway Pod for each active embedded Agent revision.
- No HorizontalPodAutoscaler or PodDisruptionBudget.

Adding tenants or Agent revisions creates more Pods and can create more nodes,
but it does not scale the OCE API, worker, or PostgreSQL. Configure and validate
application-level scaling separately before load testing.

The NodePool limits are hard compute ceilings:

| NodePool | CPU limit | Memory limit | Selected capacity |
|---|---:|---:|---|
| `oce-control` | 8 vCPU | 32 GiB | ARM64, On-Demand, 2-vCPU c/m/r instances newer than generation 6 |
| `oce-agents` | 32 vCPU | 128 GiB | ARM64, On-Demand, 2-vCPU c/m/r instances newer than generation 6 |

When a NodePool reaches its limit, matching Pods can remain Pending. The current
settings consolidate empty or underutilized nodes after one minute and expire
nodes after 168 hours. See
[Cost optimization in EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/auto-cost-control.html).

## Persistence and Availability Zones

The supplied StorageClasses use encrypted gp3 EBS, `ReadWriteOnce`,
`WaitForFirstConsumer`, and the Auto Mode EBS provisioner. The first scheduled
consumer determines the EBS Availability Zone.

| Data | Claim size | Behaviour |
|---|---:|---|
| Bootstrap output | 1 GiB | Holds the initial administrator service key; not mounted by steady-state OCE Pods |
| Development PostgreSQL | 5 GiB | Single-Pod database with no backup or failover |
| Agent gateway state and embedded workspace | 10 GiB per Agent | Survives Pod and normal node replacement while the PVC and EBS volume remain |

An EBS volume is single-AZ. A replacement Pod must run on a node in the volume's
Availability Zone. If that Zone has no matching Auto Mode capacity, subnet, or
NodePool option, the Pod remains Pending. Multiple cluster subnets do not make a
single EBS volume multi-AZ.

`ReadWriteOnce` allows one node to mount the volume read-write; it does not
provide writer fencing during a partition. Verify the old node is fenced before
forcing a replacement writer.

The StorageClasses use `reclaimPolicy: Delete`. Agent deletion can therefore
delete its volume, and cluster deletion can leave volumes that need manual
review. EBS persistence is not a backup. Add snapshots, restore tests, and a
managed multi-AZ PostgreSQL design before storing durable data.

See [EKS Auto Mode storage classes](https://docs.aws.amazon.com/eks/latest/userguide/create-storage-class.html).

## Kubernetes resource controls

The Installation template applies these controls to each generated tenant and
gateway namespace:

- Pod count: `10`.
- Aggregate requested CPU: `4`.
- Aggregate requested memory: `8Gi`.
- Aggregate CPU limits: `8`.
- Aggregate memory limits: `16Gi`.
- PVC count: `10`.
- Aggregate requested storage: `100Gi`.
- Default per-container request: `100m` CPU and `256Mi` memory.
- Default per-container limit: `2` CPU and `2Gi` memory.

Gateway and Agent containers explicitly use the same request and limit values.
The OCE API and worker request `100m` CPU and `256Mi` memory and limit each
container to `500m` CPU and `1Gi` memory.

Change `oce/installation.yaml.tmpl` before installation to set a different
tenant budget. A Pod quota alone does not cap aggregate CPU, memory, or storage.
Changing the template does not update a running installation by itself. Apply
the reviewed OCE upgrade and reconciliation procedure for existing namespaces.

`nativeOpenClawSessionCapacity: 8` bounds retained native OpenClaw sessions
inside each Agent revision. It is not a Pod replica or NodePool scaling setting.

## AWS cost and quota controls

Create an account-wide monthly cost budget for the isolated evaluation account:

```bash
MONTHLY_BUDGET_USD=100 \
BUDGET_EMAIL=operator@example.com \
  ./scripts/create-cost-budget.sh
```

The script creates forecasted 80 percent and actual 100 percent email
notifications. Set `BUDGET_NAME` to override the default
`$CLUSTER_NAME-monthly-cost` name. The script exits if that budget already
exists.

AWS Budgets is delayed monitoring, not a hard spend stop. Costs can continue
after a threshold is crossed. See
[AWS Budgets considerations](https://docs.aws.amazon.com/cost-management/latest/userguide/bcm-lite-use-budget.html).

Use these controls for different failure modes:

| Control | What it limits | What it does not limit |
|---|---|---|
| NodePool `limits` | Total provisioned CPU and memory in each custom pool | Default Auto Mode pools, EBS, NAT, ECR, Bedrock, or application replicas |
| Namespace ResourceQuota | Tenant Pod, CPU, memory, PVC, and storage requests | AWS service spend outside Kubernetes |
| Container limits | CPU and memory available to one container | Replica count or aggregate tenant spend |
| Amazon Bedrock service quotas | Maximum supported request or token throughput | Monthly cost |
| AWS Budgets | Cost visibility and notification | Immediate enforcement |

This sample does not install a Bedrock token budget, per-tenant usage meter, or
automatic shutdown action. In a shared AWS account, OCE Namespace IDs do not
create native Bedrock cost-allocation dimensions. Use separate AWS accounts or
separately metered model identities when hard tenant cost attribution matters.

The main cost drivers are the EKS cluster, Auto Mode EC2 instances, NAT gateway
hours and traffic, EBS volumes, ECR storage and scanning, CloudWatch logs, and
Bedrock model requests.
