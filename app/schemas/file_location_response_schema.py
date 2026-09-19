from pydantic import BaseModel
from typing import Dict, Any, List, Optional


class FileLocationItem(BaseModel):
    repo: str
    file_path: str
    config: Dict[str, Any]
    queue_code: Optional[str] = None
    base_branch: Optional[str] = None
    feature_branch: Optional[str] = None
    target_branch: Optional[str] = None
    script_gen_key: Optional[str] = None
    infra_type_ref: Optional[str] = None
    mode: Optional[str] = None  # "git" | "jenkins" | "kubectl"
    template_path: Optional[str] = None  # absolute path to job/script template (used by K8sJobScriptGenComponent)


class FileLocationResponse(BaseModel):
    files: List[FileLocationItem] 