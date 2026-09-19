"""
Jenkins REST API Integration

Async client for managing Jenkins pipeline jobs and builds.
Uses httpx for async HTTP calls with Basic auth (username + API token).

Jenkins connection config is stored in pipeline_vendor_mst.auth_config:
    {
        "jenkins_url": "http://jenkins.internal:8080",
        "jenkins_user": "admin",
        "jenkins_api_token": "xxx",
        "github_credentials_id": "github-app-creds"
    }
"""

import logging
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import httpx

logger = logging.getLogger(__name__)

# Timeout for Jenkins API calls (seconds)
_TIMEOUT = 30

# Jenkins job config XML template with inline pipeline script.
# Pre-declares GITHUB_TOKEN as a string parameter so that
# /buildWithParameters works from the very first build
# (without this, Jenkins returns 400 until it parses the Jenkinsfile).
#
# TODO: Migrate to GitHub Branch Source Plugin's GitHub App credentials
# (withCredentials + credential ID) so Jenkins generates tokens itself
# and we stop passing GITHUB_TOKEN from ObsTool entirely.
_PIPELINE_JOB_CONFIG_XML = """\
<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <description>{description}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions>
        <hudson.model.StringParameterDefinition>
          <name>GITHUB_TOKEN</name>
          <description>GitHub App installation token for infra repo (injected by ObsTool)</description>
          <defaultValue></defaultValue>
          <trim>false</trim>
        </hudson.model.StringParameterDefinition>
        <hudson.model.StringParameterDefinition>
          <name>SOURCE_GITHUB_TOKEN</name>
          <description>GitHub App installation token for source repo (injected by ObsTool)</description>
          <defaultValue></defaultValue>
          <trim>false</trim>
        </hudson.model.StringParameterDefinition>
      </parameterDefinitions>
    </hudson.model.ParametersDefinitionProperty>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{script}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
"""


class JenkinsIntegration:
    """Async Jenkins REST API client for job management and build control."""

    def __init__(self, jenkins_url: str, username: str, api_token: str):
        self.jenkins_url = jenkins_url.rstrip("/")
        self.username = username
        self.api_token = api_token

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.username, self.api_token)

    async def _request(
        self,
        method: str,
        path: str,
        content: Optional[str] = None,
        content_type: Optional[str] = None,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        url = f"{self.jenkins_url}{path}"
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type

        async with httpx.AsyncClient(auth=self._auth(), timeout=_TIMEOUT) as client:
            response = await client.request(
                method,
                url,
                content=content,
                headers=headers,
                params=params,
                follow_redirects=True,
            )
        return response

    @staticmethod
    def _build_job_config_xml(
        script: str,
        description: str = "",
        extra_parameters: Optional[List[Tuple[str, str, str]]] = None,
    ) -> str:
        """Render the job config XML.

        ``extra_parameters`` is an optional list of ``(name, default, description)``
        tuples appended to the job's parameter definitions. Required because
        Jenkins ignores any value sent to ``/buildWithParameters`` whose name
        isn't pre-declared on the job (the script's own ``parameters {}``
        block is only consulted *during* a build, not at trigger time).
        """
        base = _PIPELINE_JOB_CONFIG_XML.format(
            script=xml_escape(script),
            description=xml_escape(description),
        )
        if not extra_parameters:
            return base

        extra_xml = "".join(
            "        <hudson.model.StringParameterDefinition>\n"
            f"          <name>{xml_escape(name)}</name>\n"
            f"          <description>{xml_escape(desc)}</description>\n"
            f"          <defaultValue>{xml_escape(default)}</defaultValue>\n"
            "          <trim>false</trim>\n"
            "        </hudson.model.StringParameterDefinition>\n"
            for name, default, desc in extra_parameters
        )
        # Splice the extra defs in before the closing </parameterDefinitions> tag
        return base.replace(
            "      </parameterDefinitions>",
            extra_xml + "      </parameterDefinitions>",
            1,
        )

    async def job_exists(self, name: str) -> bool:
        try:
            resp = await self._request("GET", f"/job/{name}/api/json")
            return resp.status_code == 200
        except Exception:
            return False

    async def create_pipeline_job(
        self,
        name: str,
        script: str,
        description: str = "",
        extra_parameters: Optional[List[Tuple[str, str, str]]] = None,
    ) -> Dict:
        """Create a Jenkins Pipeline job with an inline Jenkinsfile script."""
        config_xml = self._build_job_config_xml(script, description, extra_parameters)
        resp = await self._request(
            "POST",
            "/createItem",
            content=config_xml,
            content_type="application/xml; charset=UTF-8",
            params={"name": name},
        )
        if resp.status_code in (200, 201, 302):
            job_url = f"{self.jenkins_url}/job/{name}"
            logger.info("Created Jenkins job: %s", job_url)
            return {"status": "success", "job_name": name, "job_url": job_url}

        logger.error("Failed to create Jenkins job %s: %s %s", name, resp.status_code, resp.text)
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text}"}

    async def update_pipeline_job(
        self,
        name: str,
        script: str,
        description: str = "",
        extra_parameters: Optional[List[Tuple[str, str, str]]] = None,
    ) -> Dict:
        """Update an existing Jenkins Pipeline job config."""
        config_xml = self._build_job_config_xml(script, description, extra_parameters)
        resp = await self._request(
            "POST",
            f"/job/{name}/config.xml",
            content=config_xml,
            content_type="application/xml; charset=UTF-8",
        )
        if resp.status_code == 200:
            logger.info("Updated Jenkins job: %s", name)
            return {"status": "success", "job_name": name}

        logger.error("Failed to update Jenkins job %s: %s", name, resp.status_code)
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text}"}

    async def create_or_update_pipeline_job(
        self,
        name: str,
        script: str,
        description: str = "",
        extra_parameters: Optional[List[Tuple[str, str, str]]] = None,
    ) -> Dict:
        """Create a job if it doesn't exist, otherwise update it."""
        if await self.job_exists(name):
            return await self.update_pipeline_job(name, script, description, extra_parameters)
        return await self.create_pipeline_job(name, script, description, extra_parameters)

    async def delete_job(self, name: str) -> Dict:
        resp = await self._request("POST", f"/job/{name}/doDelete")
        if resp.status_code in (200, 302):
            logger.info("Deleted Jenkins job: %s", name)
            return {"status": "success"}
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text}"}

    async def trigger_build(self, name: str, parameters: Optional[Dict] = None) -> Dict:
        """
        Trigger a build for the given job. Returns queue location.

        Uses /buildWithParameters when parameters are provided.
        The job XML pre-declares parameter definitions so this works
        even on the very first build.
        """
        resp = await self._request(
            "POST",
            f"/job/{name}/buildWithParameters",
            params=parameters or {},
        )

        if resp.status_code in (200, 201, 302):
            queue_url = resp.headers.get("Location", "")
            logger.info("Triggered build for %s, queue: %s", name, queue_url)
            return {"status": "success", "job_name": name, "queue_url": queue_url}

        logger.error("Failed to trigger build for %s: %s", name, resp.status_code)
        return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text}"}

    async def get_build_status(self, name: str, build_number: int) -> Dict:
        resp = await self._request("GET", f"/job/{name}/{build_number}/api/json")
        if resp.status_code == 200:
            data = resp.json()
            return {
                "status": "success",
                "building": data.get("building", False),
                "result": data.get("result"),  # SUCCESS, FAILURE, ABORTED, None (building)
                "duration": data.get("duration"),
                "url": data.get("url"),
                "number": data.get("number"),
            }
        return {"status": "error", "message": f"HTTP {resp.status_code}"}

    async def get_last_build_status(self, name: str) -> Dict:
        resp = await self._request("GET", f"/job/{name}/lastBuild/api/json")
        if resp.status_code == 200:
            data = resp.json()
            return {
                "status": "success",
                "building": data.get("building", False),
                "result": data.get("result"),
                "number": data.get("number"),
                "url": data.get("url"),
                "duration": data.get("duration"),
                "timestamp": data.get("timestamp"),
            }
        if resp.status_code == 404:
            return {"status": "success", "result": "NOT_BUILT", "building": False}
        return {"status": "error", "message": f"HTTP {resp.status_code}"}

    async def get_build_console_log(self, name: str, build_number: int) -> str:
        resp = await self._request("GET", f"/job/{name}/{build_number}/consoleText")
        if resp.status_code == 200:
            return resp.text
        return f"Failed to fetch console log: HTTP {resp.status_code}"

    async def get_build_console_log_progressive(
        self, name: str, build_number: int, start: int = 0
    ) -> Dict:
        """
        Fetch console log progressively using Jenkins progressive text API.

        Returns:
            {text: str, next_offset: int, has_more: bool}
        """
        resp = await self._request(
            "GET",
            f"/job/{name}/{build_number}/logText/progressiveText",
            params={"start": str(start)},
        )
        if resp.status_code == 200:
            next_offset = int(resp.headers.get("X-Text-Size", start))
            has_more = resp.headers.get("X-More-Data", "").lower() == "true"
            return {
                "text": resp.text,
                "next_offset": next_offset,
                "has_more": has_more,
            }
        return {"text": "", "next_offset": start, "has_more": False}
