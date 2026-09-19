"""GPU Configuration endpoint — returns AWS GPU instance specs for the frontend."""

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import List, Dict

from app.core.aws_gpu_config import GPU_CONFIGS, GPU_TYPE_ORDER

router = APIRouter()


class InstanceSizeResponse(BaseModel):
    name: str
    gpus: int
    vcpus: int
    memory_gb: int


class GpuTypeResponse(BaseModel):
    gpu_name: str
    instance_family: str
    vram_gb: int
    instances: List[InstanceSizeResponse]
    valid_gpu_counts: List[int]


class GpuConfigResponse(BaseModel):
    gpu_types: Dict[str, GpuTypeResponse] = Field(
        ..., description="GPU type configs keyed by GPU name"
    )
    gpu_type_order: List[str] = Field(
        ..., description="Ordered list of GPU types for UI display"
    )


@router.get("", response_model=GpuConfigResponse, summary="Get GPU Instance Configs")
async def get_gpu_configs():
    """Return all GPU types with their EC2 instance families and sizes.

    No auth required — this is static reference data.
    """
    gpu_types = {}
    for gpu_name in GPU_TYPE_ORDER:
        config = GPU_CONFIGS[gpu_name]
        gpu_types[gpu_name] = GpuTypeResponse(
            gpu_name=config.gpu_name,
            instance_family=config.instance_family,
            vram_gb=config.vram_gb,
            instances=[
                InstanceSizeResponse(
                    name=i.name,
                    gpus=i.gpus,
                    vcpus=i.vcpus,
                    memory_gb=i.memory_gb,
                )
                for i in config.instances
            ],
            valid_gpu_counts=config.valid_gpu_counts,
        )

    return GpuConfigResponse(
        gpu_types=gpu_types,
        gpu_type_order=list(GPU_TYPE_ORDER),
    )
