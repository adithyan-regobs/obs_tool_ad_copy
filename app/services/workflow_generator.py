"""
GitHub Actions Workflow Generator for CI/CD templates.
Generates workflow YAML based on language, enabled steps, and infrastructure.
"""
from typing import List, Dict, Any, Optional
import os
import aiofiles


def generate_workflow_yaml(
    language: str,
    steps: List[Dict[str, Any]],
    service_name: str = "my-service",
    environment: str = "dev",
    branches: List[str] = None,
    build_path: Optional[str] = None,
    dockerfile_path: Optional[str] = None,
    additional_trigger_paths: Optional[List[str]] = None,
    infrastructure_type: str = "eks",
    aws_region: str = "us-east-1",
    eks_cluster_name: str = "my-cluster"
) -> str:
    """
    Generate GitHub Actions workflow YAML based on language and enabled steps.

    Args:
        language: Programming language (go, java, nodejs, python)
        steps: List of workflow step configurations (only enabled steps will be included)
        service_name: Name of the service
        environment: Environment (dev, staging, prod)
        branches: List of branches to trigger on
        build_path: Path to build directory (triggers workflow when changed)
        dockerfile_path: Path to Dockerfile (triggers workflow when changed)
        additional_trigger_paths: Additional paths that trigger the workflow
        infrastructure_type: Type of infrastructure (eks, ecs)
        aws_region: AWS region for deployment
        eks_cluster_name: Name of EKS cluster

    Returns:
        GitHub Actions workflow YAML string
    """
    if branches is None:
        branches = ["main"]

    # Filter to only enabled steps
    enabled_steps = [s for s in steps if s.get("enabled", False)]

    # Start building the workflow YAML
    yaml_lines = []

    # Workflow header
    yaml_lines.append(f"name: Deploy {service_name} to {environment.lower()}")
    yaml_lines.append("")
    yaml_lines.append("run-name: >-")
    yaml_lines.append("  ${{ github.event.inputs.triggered_by_user != '' &&")
    yaml_lines.append(f"      format('Deploy {service_name} to {environment.lower()} (requested by {{0}})', github.event.inputs.triggered_by_user)")
    yaml_lines.append("      ||")
    yaml_lines.append(f"      format('Deploy {service_name} to {environment.lower()}')")
    yaml_lines.append("  }}")
    yaml_lines.append("")

    # Workflow triggers
    yaml_lines.append("on:")
    yaml_lines.append("  push:")
    yaml_lines.append("    branches:")

    for branch in branches:
        yaml_lines.append(f"      - {branch}")

    # Add path filters if provided
    path_filters = generate_path_filters(build_path, dockerfile_path, additional_trigger_paths)
    if path_filters:
        yaml_lines.append(path_filters)

    yaml_lines.append("  workflow_dispatch:")
    yaml_lines.append("    inputs:")
    yaml_lines.append("      triggered_by_user:")
    yaml_lines.append('        description: "Email or name of the user triggering this workflow"')
    yaml_lines.append("        required: true")
    yaml_lines.append("")

    # Jobs section
    yaml_lines.append("jobs:")

    # Get step IDs in order
    step_ids = [s["id"] for s in enabled_steps]

    # Build dependency map
    dependencies = {}
    for step in enabled_steps:
        step_id = step["id"]
        depends_on = step.get("dependsOn", [])
        dependencies[step_id] = depends_on

    # Generate jobs for each enabled step
    for i, step in enumerate(enabled_steps):
        step_id = step["id"]
        step_name = step["name"]
        step_category = step.get("category", "")

        # Build the job needs clause based on dependencies
        needs = dependencies[step_id] if dependencies[step_id] else []
        needs_clause = f"needs: [{', '.join(needs)}]" if needs else ""

        # Generate job based on step type
        job_yaml = generate_job_for_step(
            step=step,
            service_name=service_name,
            environment=environment,
            language=language,
            aws_region=aws_region,
            eks_cluster_name=eks_cluster_name,
            infrastructure_type=infrastructure_type,
            needs_clause=needs_clause
        )

        yaml_lines.append(job_yaml)

    return "\n".join(yaml_lines)


def generate_job_for_step(
    step: Dict[str, Any],
    service_name: str,
    environment: str,
    language: str,
    aws_region: str,
    eks_cluster_name: str,
    infrastructure_type: str,
    needs_clause: str = ""
) -> str:
    """
    Generate a GitHub Actions job for a specific workflow step.

    Args:
        step: Step configuration dictionary
        service_name: Name of the service
        environment: Environment (dev, staging, prod)
        language: Programming language
        aws_region: AWS region
        eks_cluster_name: EKS cluster name
        infrastructure_type: Type of infrastructure (eks, ecs)
        needs_clause: GitHub Actions needs clause for dependencies

    Returns:
        YAML formatted job
    """
    step_id = step["id"]
    step_name = step["name"]

    job_lines = []
    job_lines.append(f"  {step_id}:")
    job_lines.append(f"    name: {step_name}")

    if needs_clause:
        job_lines.append(f"    {needs_clause}")

    job_lines.append("    runs-on: ubuntu-latest")
    job_lines.append("    steps:")

    # Add checkout step (most jobs need this)
    if step["id"] not in ["load-config"]:
        job_lines.append("      - name: Checkout code")
        job_lines.append("        uses: actions/checkout@v4")
        job_lines.append("        with:")
        job_lines.append("          fetch-depth: 0")
        job_lines.append("")

    # Generate step-specific content based on step ID
    if step_id == "code-checkout":
        job_lines.append("      - name: Checkout code")
        job_lines.append("        uses: actions/checkout@v4")
        job_lines.append("        with:")
        job_lines.append("          fetch-depth: 0")

    elif step_id == "code-quality-check":
        # Add code quality steps based on language
        code_quality_steps = generate_code_quality_steps(language)
        if code_quality_steps:
            # Add indentation to match job level
            indented_steps = code_quality_steps.split("\n")
            for line in indented_steps:
                if line.strip():
                    job_lines.append(f"    {line}")

    elif step_id == "build":
        # Add build setup steps
        build_setup = generate_build_setup_steps(language)
        if build_setup:
            indented_steps = build_setup.split("\n")
            for line in indented_steps:
                if line.strip():
                    job_lines.append(f"    {line}")
            job_lines.append("")

        # Add build commands
        job_lines.append("      - name: Build application")
        job_lines.append("        run: |")
        job_lines.append(f"          echo \"Building {service_name} for {environment.lower()} environment...\"")

        build_commands = generate_build_commands(language)
        if build_commands:
            indented_cmds = build_commands.split("\n")
            for line in indented_cmds:
                if line.strip():
                    job_lines.append(f"        {line}")

    elif step_id == "docker-build":
        job_lines.append("      - name: Set up Docker Buildx")
        job_lines.append("        uses: docker/setup-buildx-action@v3")
        job_lines.append("")
        job_lines.append("      - name: Build Docker image")
        job_lines.append("        run: |")
        job_lines.append(f"          echo \"Building Docker image for {service_name}...\"")
        job_lines.append("          docker build -t ${{ github.repository }}:${{ github.sha }} .")
        job_lines.append("          docker tag ${{ github.repository }}:${{ github.sha }} ${{ github.repository }}:latest")

    elif step_id == "generate-sbom":
        job_lines.append("      - name: Generate SBOM")
        job_lines.append("        run: |")
        job_lines.append("          echo \"Generating Software Bill of Materials...\"")
        job_lines.append("          # Add SBOM generation command here")
        job_lines.append("          # Example: syft ${{ github.repository }}:${{ github.sha }} -o cyclonedx-json > sbom.json")

    elif step_id == "image-security-check":
        job_lines.append("      - name: Scan Docker image")
        job_lines.append("        run: |")
        job_lines.append("          echo \"Running Trivy vulnerability scanner...\"")
        job_lines.append("          # Add Trivy scan command here")
        job_lines.append("          # Example: trivy image ${{ github.repository }}:latest")

    elif step_id == "pre-security-validation" or step_id == "post-security":
        job_lines.append("      - name: Run Snyk security scan")
        job_lines.append("        run: |")
        job_lines.append("          echo \"Running Snyk security scan...\"")
        job_lines.append("          # Add Snyk scan command here")
        job_lines.append("          # Example: snyk test --json > snyk-results.json")

    elif step_id == "deploy":
        if infrastructure_type.lower() == "eks":
            job_lines.append("      - name: Configure AWS credentials")
            job_lines.append("        uses: aws-actions/configure-aws-credentials@v4")
            job_lines.append("        with:")
            job_lines.append("          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}")
            job_lines.append("          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}")
            job_lines.append(f"          aws-region: {aws_region}")
            job_lines.append("")
            job_lines.append("      - name: Update kubeconfig")
            job_lines.append("        run: |")
            job_lines.append(f"          aws eks update-kubeconfig --name {eks_cluster_name} --region {aws_region}")
            job_lines.append("")
            job_lines.append("      - name: Deploy to EKS")
            job_lines.append("        run: |")
            job_lines.append(f"          echo \"Deploying {service_name} to {environment.lower()} environment in EKS cluster...\"")
            job_lines.append("          # kubectl set image deployment/${{ github.repository }} ${{ github.repository }}=${{ github.sha }}")
            job_lines.append("          # kubectl rollout status deployment/${{ github.repository }}")

        elif infrastructure_type.lower() == "ecs":
            job_lines.append("      - name: Configure AWS credentials")
            job_lines.append("        uses: aws-actions/configure-aws-credentials@v4")
            job_lines.append("        with:")
            job_lines.append("          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}")
            job_lines.append("          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}")
            job_lines.append(f"          aws-region: {aws_region}")
            job_lines.append("")
            job_lines.append("      - name: Deploy to ECS")
            job_lines.append("        run: |")
            job_lines.append(f"          echo \"Deploying {service_name} to {environment.lower()} environment in ECS...\"")
            job_lines.append("          # Add ECS deployment commands here")

    elif step_id == "notify":
        job_lines.append("      - name: Send notification")
        job_lines.append("        if: always()")
        job_lines.append("        run: |")
        job_lines.append(f"          echo \"Sending notification for {service_name} deployment...\"")
        job_lines.append("          # Slack notification logic here")

    else:
        # Generic step
        job_lines.append(f"      - name: {step_name}")
        job_lines.append("        run: |")
        job_lines.append(f"          echo \"Executing {step_name}...\"")

    return "\n".join(job_lines)


def get_template_path(infrastructure_type: str) -> str:
    """
    Get the path to the workflow template based on infrastructure type.

    Args:
        infrastructure_type: Type of infrastructure (eks, ecs)

    Returns:
        Path to the template file
    """
    # Get the base directory of the app
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if infrastructure_type.lower() == "eks":
        template_path = os.path.join(base_dir, "templates", "eks", "workflow", "workflow-template.yml")
    elif infrastructure_type.lower() == "ecs":
        template_path = os.path.join(base_dir, "templates", "ecs", "workflow", "workflow-template.yml")
    else:
        # Default to EKS template
        template_path = os.path.join(base_dir, "templates", "eks", "workflow", "workflow-template.yml")

    # If template doesn't exist, create a basic one
    if not os.path.exists(template_path):
        return create_basic_template()

    return template_path


def create_basic_template() -> str:
    """
    Create a basic inline template if file doesn't exist.

    Returns:
        Template content as string
    """
    return """name: Deploy {{SERVICE_NAME}} to {{ENVIRONMENT}}

run-name: >-
  ${{ github.event.inputs.triggered_by_user != '' &&
      format('Deploy {{SERVICE_NAME}} to {{ENVIRONMENT}} (requested by {0})', github.event.inputs.triggered_by_user)
      ||
      format('Deploy {{SERVICE_NAME}} to {{ENVIRONMENT}}')
  }}

on:
  push:
    branches:
      {{BRANCHES}}
{{PATH_FILTERS}}
  workflow_dispatch:
    inputs:
      triggered_by_user:
        description: "Email or name of the user triggering this workflow"
        required: true

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Code Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

{{CODE_QUALITY_STEPS}}

{{BUILD_STEPS}}

      - name: Build application
        run: |
{{BUILD_COMMANDS}}

      - name: Docker Build
        run: |
          echo "Building Docker image..."
          docker build -t ${{ github.repository }}:${{ github.sha }} .
          docker tag ${{ github.repository }}:${{ github.sha }} ${{ github.repository }}:latest

{{SECURITY_STEPS}}

      - name: Send Notification
        if: always()
        run: |
          echo "Sending notification..."
"""


async def read_template(template_path: str) -> str:
    """
    Read the template file content.

    Args:
        template_path: Path to the template file

    Returns:
        Template content as string
    """
    try:
        async with aiofiles.open(template_path, 'r') as f:
            return await f.read()
    except Exception as e:
        print(f"Error reading template file {template_path}: {e}")
        return create_basic_template()


def generate_branches_list(branches: List[str]) -> str:
    """
    Generate YAML list of branches.

    Args:
        branches: List of branch names

    Returns:
        YAML formatted branch list
    """
    yaml_lines = []
    for branch in branches:
        yaml_lines.append(f"      - {branch}")
    return "\n".join(yaml_lines)


def generate_path_filters(
    build_path: Optional[str],
    dockerfile_path: Optional[str],
    additional_paths: Optional[List[str]]
) -> str:
    """
    Generate path filters for workflow triggers.

    Args:
        build_path: Path to build directory
        dockerfile_path: Path to Dockerfile
        additional_paths: Additional trigger paths

    Returns:
        YAML formatted path filters
    """
    all_paths = []

    if build_path:
        all_paths.append(build_path)

    if dockerfile_path:
        all_paths.append(dockerfile_path)

    if additional_paths:
        all_paths.extend(additional_paths)

    if not all_paths:
        return ""

    # Generate path filters YAML
    yaml_lines = ["    paths:"]
    for path in all_paths:
        yaml_lines.append(f"      - '{path}'")

    return "\n".join(yaml_lines)


def generate_code_quality_steps(language: str) -> str:
    """
    Generate code quality steps based on language.

    Args:
        language: Programming language

    Returns:
        YAML formatted code quality steps
    """
    lang = language.lower()

    if lang in ["go", "golang"]:
        return """      - name: Run go fmt
        run: |
          go fmt ./...
          go vet ./...

      - name: Run golangci-lint
        run: |
          go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
          golangci-lint run --timeout=5m"""

    elif lang in ["java", "maven", "gradle", "java gradle", "java maven"]:
        return """      - name: Run Checkstyle
        run: mvn checkstyle:check

      - name: Run SpotBugs
        run: mvn spotbugs:check

      - name: Run PMD
        run: mvn pmd:check"""

    elif lang in ["nodejs", "node", "javascript", "typescript"]:
        return """      - name: Run ESLint
        run: npm run lint

      - name: Run Prettier check
        run: npm run format:check"""

    elif lang in ["python", "python3"]:
        return """      - name: Run pylint
        run: |
          pip install pylint
          pylint **/*.py

      - name: Run black check
        run: |
          pip install black
          black --check .

      - name: Run mypy
        run: |
          pip install mypy
          mypy ."""

    else:
        return """      - name: Code Quality Check
        run: |
          echo "Running code quality checks..."
          echo "No language-specific checks configured"
"""


def generate_build_setup_steps(language: str) -> str:
    """
    Generate build setup steps based on language.

    Args:
        language: Programming language

    Returns:
        YAML formatted build setup steps
    """
    lang = language.lower()

    if lang in ["go", "golang"]:
        return """      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version: '1.23'
          cache: true

      - name: Download dependencies
        run: |
          go mod download
          go mod verify"""

    elif lang in ["java", "maven", "gradle", "java gradle", "java maven"]:
        return """      - name: Set up JDK
        uses: actions/setup-java@v4
        with:
          distribution: 'temurin'
          java-version: '21'
          cache: 'maven'

      - name: Download dependencies
        run: mvn dependency:go-offline"""

    elif lang in ["nodejs", "node", "javascript", "typescript"]:
        return """      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'

      - name: Install dependencies
        run: npm ci"""

    elif lang in ["python", "python3"]:
        return """      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt"""

    else:
        return """      - name: Setup environment
        run: |
          echo "Setting up build environment..."
          echo "No language-specific setup configured"
"""


def generate_build_commands(language: str) -> str:
    """
    Generate build commands based on language.

    Args:
        language: Programming language

    Returns:
        YAML formatted build commands
    """
    lang = language.lower()

    if lang in ["go", "golang"]:
        return """          echo "Building Go application..."
          CGO_ENABLED=0 GOOS=linux go build -a -installsuffix cgo -o bin/main ./cmd/api"""

    elif lang in ["java", "maven", "java maven"]:
        return """          echo "Building Java application with Maven..."
          mvn clean package -DskipTests"""

    elif lang in ["java", "gradle", "java gradle"]:
        return """          echo "Building Java application with Gradle..."
          ./gradlew clean build"""

    elif lang in ["nodejs", "node", "javascript", "typescript"]:
        return """          echo "Building Node.js application..."
          npm run build"""

    elif lang in ["python", "python3"]:
        return """          echo "Building Python application..."
          python -m build"""

    else:
        return """          echo "Building application..."
          echo "No language-specific build command configured"
"""


def generate_security_steps(steps: List[Dict[str, Any]]) -> str:
    """
    Generate security scanning steps based on enabled steps.

    Args:
        steps: List of workflow step configurations

    Returns:
        YAML formatted security steps
    """
    enabled_step_ids = [s.get("id") for s in steps if s.get("enabled", False)]

    security_steps = []

    if "generate-sbom" in enabled_step_ids:
        security_steps.append("""      - name: Generate SBOM
        run: |
          echo "Generating Software Bill of Materials..."
          # Add SBOM generation command here
          # Example: syft ${{ github.repository }}:${{ github.sha }} -o cyclonedx-json > sbom.json""")

    if "image-security-check" in enabled_step_ids:
        security_steps.append("""      - name: Scan Docker image
        run: |
          echo "Running Trivy vulnerability scanner..."
          # Add Trivy scan command here
          # Example: trivy image ${{ github.repository }}:latest""")

    if "pre-security-validation" in enabled_step_ids or "post-security" in enabled_step_ids:
        security_steps.append("""      - name: Run Snyk security scan
        run: |
          echo "Running Snyk security scan..."
          # Add Snyk scan command here
          # Example: snyk test --json > snyk-results.json""")

    if not security_steps:
        return """      - name: Security Scan
        run: |
          echo "Running security scan..."
          echo "No security scanning tools configured"""

    return "\n\n".join(security_steps)


def get_predefined_templates(language: str) -> List[Dict[str, Any]]:
    """
    Get predefined workflow templates for a language.

    Args:
        language: Programming language

    Returns:
        List of predefined templates
    """
    from app.types.workflowSteps import get_workflow_steps

    all_steps = get_workflow_steps(language)

    templates = [
        {
            "name": "Quick Release",
            "description": "Fast deployment with basic quality checks",
            "template_type": "quick_release",
            "steps": [
                {**s, "enabled": s["id"] in ["code-checkout", "code-quality-check", "build", "docker-build"]}
                for s in all_steps
            ]
        },
        {
            "name": "Full Release",
            "description": "Complete pipeline with all security and quality checks",
            "template_type": "full_release",
            "steps": [{**s, "enabled": True} for s in all_steps]
        },
        {
            "name": "Custom Workflow",
            "description": "Create your own workflow by selecting steps",
            "template_type": "custom",
            "steps": all_steps
        }
    ]

    return templates
