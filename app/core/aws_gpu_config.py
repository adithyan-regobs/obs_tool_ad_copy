"""
AWS GPU Instance Configuration

Single source of truth for GPU type → EC2 instance family mappings.
Used by the model-serving Jenkinsfile generator (backend) and mirrored
in the frontend for UI validation.

Data verified against AWS EC2 describe-instance-types API (April 2026).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class InstanceSize:
    """A specific EC2 instance size within a GPU family."""
    name: str
    gpus: int
    vcpus: int
    memory_gb: int  # Instance RAM in GiB


@dataclass(frozen=True)
class GpuType:
    """Configuration for a GPU type and its EC2 instance family."""
    gpu_name: str           # e.g. "L4", "A10G"
    instance_family: str    # e.g. "g6", "g5"
    vram_gb: int            # GPU memory per GPU in GB
    instances: List[InstanceSize] = field(default_factory=list)

    @property
    def valid_gpu_counts(self) -> List[int]:
        """Sorted list of distinct GPU counts available in this family."""
        return sorted(set(i.gpus for i in self.instances))

    @property
    def max_gpus(self) -> int:
        return max(i.gpus for i in self.instances)

    @property
    def min_gpus(self) -> int:
        return min(i.gpus for i in self.instances)


# ─── Instance Data (from AWS API, April 2026) ────────────────────────────────

GPU_CONFIGS: Dict[str, GpuType] = {
    # "T4": GpuType(
    #     gpu_name="T4",
    #     instance_family="g4dn",
    #     vram_gb=16,
    #     instances=[
    #         InstanceSize("g4dn.xlarge",   gpus=1, vcpus=4,  memory_gb=16),
    #         InstanceSize("g4dn.2xlarge",  gpus=1, vcpus=8,  memory_gb=32),
    #         InstanceSize("g4dn.4xlarge",  gpus=1, vcpus=16, memory_gb=64),
    #         InstanceSize("g4dn.8xlarge",  gpus=1, vcpus=32, memory_gb=128),
    #         InstanceSize("g4dn.16xlarge", gpus=1, vcpus=64, memory_gb=256),
    #         InstanceSize("g4dn.12xlarge", gpus=4, vcpus=48, memory_gb=192),
    #         InstanceSize("g4dn.metal",    gpus=8, vcpus=96, memory_gb=384),
    #     ],
    # ),
    "L4": GpuType(
        gpu_name="L4",
        instance_family="g6",
        vram_gb=24,
        instances=[
            InstanceSize("g6.xlarge",   gpus=1, vcpus=4,   memory_gb=16),
            InstanceSize("g6.2xlarge",  gpus=1, vcpus=8,   memory_gb=32),
            InstanceSize("g6.4xlarge",  gpus=1, vcpus=16,  memory_gb=64),
            InstanceSize("g6.8xlarge",  gpus=1, vcpus=32,  memory_gb=128),
            InstanceSize("g6.16xlarge", gpus=1, vcpus=64,  memory_gb=256),
            InstanceSize("g6.12xlarge", gpus=4, vcpus=48,  memory_gb=192),
            InstanceSize("g6.24xlarge", gpus=4, vcpus=96,  memory_gb=384),
            InstanceSize("g6.48xlarge", gpus=8, vcpus=192, memory_gb=768),
        ],
    ),
    "L4-HM": GpuType(
        gpu_name="L4-HM",
        instance_family="gr6",
        vram_gb=24,
        instances=[
            # gr6 = L4 GPU with ~8x instance RAM vs g6 equivalents
            InstanceSize("gr6.4xlarge", gpus=1, vcpus=16, memory_gb=256),
            InstanceSize("gr6.8xlarge", gpus=1, vcpus=32, memory_gb=512),
        ],
    ),
    "A10G": GpuType(
        gpu_name="A10G",
        instance_family="g5",
        vram_gb=24,
        instances=[
            InstanceSize("g5.xlarge",   gpus=1, vcpus=4,   memory_gb=16),
            InstanceSize("g5.2xlarge",  gpus=1, vcpus=8,   memory_gb=32),
            InstanceSize("g5.4xlarge",  gpus=1, vcpus=16,  memory_gb=64),
            InstanceSize("g5.8xlarge",  gpus=1, vcpus=32,  memory_gb=128),
            InstanceSize("g5.16xlarge", gpus=1, vcpus=64,  memory_gb=256),
            InstanceSize("g5.12xlarge", gpus=4, vcpus=48,  memory_gb=192),
            InstanceSize("g5.24xlarge", gpus=4, vcpus=96,  memory_gb=384),
            InstanceSize("g5.48xlarge", gpus=8, vcpus=192, memory_gb=768),
        ],
    ),
    "L40S": GpuType(
        gpu_name="L40S",
        instance_family="g6e",
        vram_gb=48,
        instances=[
            InstanceSize("g6e.xlarge",   gpus=1, vcpus=4,   memory_gb=32),
            InstanceSize("g6e.2xlarge",  gpus=1, vcpus=8,   memory_gb=64),
            InstanceSize("g6e.4xlarge",  gpus=1, vcpus=16,  memory_gb=128),
            InstanceSize("g6e.8xlarge",  gpus=1, vcpus=32,  memory_gb=256),
            InstanceSize("g6e.16xlarge", gpus=1, vcpus=64,  memory_gb=512),
            InstanceSize("g6e.12xlarge", gpus=4, vcpus=48,  memory_gb=384),
            InstanceSize("g6e.24xlarge", gpus=4, vcpus=96,  memory_gb=768),
            InstanceSize("g6e.48xlarge", gpus=8, vcpus=192, memory_gb=1536),
        ],
    ),
    "A100": GpuType(
        gpu_name="A100",
        instance_family="p4d",
        vram_gb=40,
        instances=[
            # A100 40 GB — only available as 8-GPU instance
            InstanceSize("p4d.24xlarge", gpus=8, vcpus=96, memory_gb=1152),
        ],
    ),
    "H100": GpuType(
        gpu_name="H100",
        instance_family="p5",
        vram_gb=80,
        instances=[
            InstanceSize("p5.4xlarge",  gpus=1,  vcpus=16,  memory_gb=256),
            InstanceSize("p5.48xlarge", gpus=8,  vcpus=192, memory_gb=2048),
        ],
    ),
}

# Ordered list for UI display (cheapest → most powerful)
GPU_TYPE_ORDER: List[str] = ["L4", "L4-HM", "A10G", "L40S", "A100", "H100"]


# ─── Helper Functions ─────────────────────────────────────────────────────────

def get_gpu_config(gpu_type: str) -> GpuType:
    """Get the full config for a GPU type. Raises KeyError if unknown."""
    return GPU_CONFIGS[gpu_type]


def get_valid_gpu_counts(gpu_type: str) -> List[int]:
    """Return sorted list of valid GPU counts for a GPU type."""
    return get_gpu_config(gpu_type).valid_gpu_counts


def get_instance_for_gpu(gpu_type: str, gpu_count: int) -> InstanceSize:
    """Return the smallest instance that has >= gpu_count GPUs.

    If the exact gpu_count isn't available, rounds up to the next valid count.
    """
    config = get_gpu_config(gpu_type)
    # Filter instances with enough GPUs, then pick smallest by vcpus
    candidates = [i for i in config.instances if i.gpus >= gpu_count]
    if not candidates:
        raise ValueError(
            f"No {gpu_type} instance supports {gpu_count} GPUs. "
            f"Max is {config.max_gpus}."
        )
    return min(candidates, key=lambda i: (i.gpus, i.vcpus))


def get_resource_bounds(gpu_type: str, gpu_count: int) -> Dict[str, int]:
    """Return min/max CPU and memory for a GPU type + count combination.

    Min = smallest instance with >= gpu_count GPUs (pod floor).
    Max = largest instance with that same GPU count (pod ceiling).
    """
    config = get_gpu_config(gpu_type)
    # Find the actual GPU count that will be used (round up)
    actual_gpu_count = get_instance_for_gpu(gpu_type, gpu_count).gpus
    # All instances with exactly that GPU count
    matching = [i for i in config.instances if i.gpus == actual_gpu_count]
    # Reserve headroom for kubelet kube-reserved + eviction threshold + sidecars.
    # CPU: 2 vCPUs covers kubelet overhead and Knative queue-proxy (~146m).
    # Memory: EKS reserves ~2-4% on large nodes (e.g. ~22 GiB on 768 GiB);
    #   max(4, memory_gb // 32) approximates that without a per-instance lookup.
    max_mem = max(i.memory_gb for i in matching)
    mem_headroom = max(4, max_mem // 32)
    return {
        "min_cpu": 1,
        "max_cpu": max(1, max(i.vcpus for i in matching) - 2),
        "min_memory_gb": 1,
        "max_memory_gb": max(1, max_mem - mem_headroom),
        "actual_gpu_count": actual_gpu_count,
    }


def clamp_resources(
    gpu_type: str,
    gpu_count: int,
    requested_cpu: int,
    requested_memory_gb: int,
) -> Tuple[int, int, int]:
    """Clamp CPU and memory to the valid range for a GPU type + count.

    Returns (clamped_cpu, clamped_memory_gb, actual_gpu_count).
    Values below the minimum are raised; values above the maximum are lowered.
    """
    bounds = get_resource_bounds(gpu_type, gpu_count)
    clamped_cpu = max(bounds["min_cpu"], min(requested_cpu, bounds["max_cpu"]))
    clamped_mem = max(bounds["min_memory_gb"], min(requested_memory_gb, bounds["max_memory_gb"]))
    return clamped_cpu, clamped_mem, bounds["actual_gpu_count"]


def get_best_fit_instance(
    gpu_type: str,
    gpu_count: int,
    cpu: int,
    memory_gb: int,
) -> InstanceSize:
    """Return the smallest instance that fits the requested CPU, memory, and GPU count.

    Matches instances with exactly the resolved GPU count, then picks the
    smallest one whose vcpus >= cpu AND memory_gb >= memory_gb.
    Falls back to the largest instance with that GPU count if nothing fits.
    """
    config = get_gpu_config(gpu_type)
    actual_gpu_count = get_instance_for_gpu(gpu_type, gpu_count).gpus
    matching = [i for i in config.instances if i.gpus == actual_gpu_count]
    # Filter to instances that can actually satisfy the request
    fitting = [i for i in matching if i.vcpus >= cpu and i.memory_gb >= memory_gb]
    if fitting:
        return min(fitting, key=lambda i: (i.vcpus, i.memory_gb))
    # Nothing fits — return the largest available (clamped resources will handle it)
    return max(matching, key=lambda i: (i.vcpus, i.memory_gb))


def get_fallback_instances(
    gpu_type: str,
    gpu_count: int,
    cpu: int,
    memory_gb: int,
) -> List[InstanceSize]:
    """Return an ordered list of instances that fit, smallest first.

    Used to build node affinity with weighted preferences so EKS Auto Mode
    falls back to the next-best instance if the ideal one is unavailable.
    """
    config = get_gpu_config(gpu_type)
    actual_gpu_count = get_instance_for_gpu(gpu_type, gpu_count).gpus
    matching = [i for i in config.instances if i.gpus == actual_gpu_count]
    fitting = [i for i in matching if i.vcpus >= cpu and i.memory_gb >= memory_gb]
    if not fitting:
        return sorted(matching, key=lambda i: (i.vcpus, i.memory_gb))
    return sorted(fitting, key=lambda i: (i.vcpus, i.memory_gb))


def get_node_selector(
    gpu_type: str,
    gpu_count: int = 1,
    cpu: int = 0,
    memory_gb: int = 0,
) -> Dict[str, str]:
    """Return the K8s nodeSelector dict to target the exact instance type.

    When cpu/memory are provided, pins to the smallest instance that fits.
    Falls back to instance-family only when cpu/memory are not specified.

    Uses eks.amazonaws.com/ labels (required by EKS Auto Mode).
    karpenter.k8s.aws/ labels are blocked by EKS Auto Mode's provisioning logic.
    """
    config = get_gpu_config(gpu_type)
    if cpu > 0 and memory_gb > 0:
        best = get_best_fit_instance(gpu_type, gpu_count, cpu, memory_gb)
        return {"node.kubernetes.io/instance-type": best.name}
    return {"eks.amazonaws.com/instance-family": config.instance_family}


def get_default_resources(gpu_type: str, gpu_count: int) -> Dict[str, str]:
    """Return sensible default K8s resource requests for a GPU type + count.

    Defaults to the smallest instance that fits the GPU count.
    """
    instance = get_instance_for_gpu(gpu_type, gpu_count)
    mem_headroom = max(4, instance.memory_gb // 32)
    safe_mem = max(1, instance.memory_gb - mem_headroom)
    return {
        "cpu_requested": str(max(1, instance.vcpus - 2)),
        "cpu_limit": str(max(1, instance.vcpus - 2)),
        "memory_requested": f"{safe_mem}Gi",
        "memory_limit": f"{safe_mem}Gi",
        "instance_family": get_gpu_config(gpu_type).instance_family,
    }
