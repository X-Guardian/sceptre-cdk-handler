import json
import logging
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional, Dict, Type

import aws_cdk
from aws_cdk.cx_api import CloudAssembly
from botocore.credentials import Credentials
from cdk_bootstrapless_synthesizer import BootstraplessStackSynthesizer
from sceptre import exceptions
from sceptre.connection_manager import ConnectionManager
from sceptre.exceptions import TemplateHandlerArgumentsInvalidError


class SceptreCdkStack(aws_cdk.Stack):
    def __init__(self, scope: aws_cdk.App, id: str, sceptre_user_data: Any, **kwargs):
        super().__init__(scope, id, **kwargs)
        self.sceptre_user_data = sceptre_user_data


class CdkBuilder(ABC):
    STACK_LOGICAL_ID = 'CDKStack'

    @abstractmethod
    def build_template(
        self,
        stack_class: Type[SceptreCdkStack],
        cdk_context: Optional[dict],
        sceptre_user_data: Any
    ) -> dict: ...


class BootstrappedCdkBuilder(CdkBuilder):

    def __init__(
        self,
        logger: logging.Logger,
        connection_manager: ConnectionManager,
        *,
        subprocess_run=subprocess.run,
        app_class=aws_cdk.App,
        environment_variables=os.environ
    ):
        self._logger = logger
        self._connection_manager = connection_manager
        self._subprocess_run = subprocess_run
        self._app_class = app_class
        self._environment_variables = environment_variables

    def build_template(
        self,
        stack_class: Type[SceptreCdkStack],
        cdk_context: Optional[dict],
        sceptre_user_data: Any
    ) -> dict:
        assembly = self._synthesize(stack_class, cdk_context, sceptre_user_data)
        self._publish(assembly)
        template = self._get_template(assembly)
        return template

    def _synthesize(
        self,
        stack_class: Type[SceptreCdkStack],
        cdk_context: Optional[dict],
        sceptre_user_data: Any
    ) -> CloudAssembly:
        self._logger.debug(f'CDK synthesing CdkStack Class')
        self._logger.debug(f'CDK Context: {cdk_context}')
        app = self._app_class(context=cdk_context)
        stack_class(app, self.STACK_LOGICAL_ID, sceptre_user_data)
        return app.synth()

    def _publish(self, cloud_assembly: CloudAssembly):
        asset_artifacts = self._get_assets_manifest(cloud_assembly)
        if self._only_asset_is_template(asset_artifacts):
            # Sceptre already has a mechanism to upload the template if configured. We don't
            # need to deploy assets if the only asset is the template
            self._logger.debug("Only asset is template; Skipping asset upload.")
            return

        environment_variables = self._get_envs()
        self._logger.info(f'Publishing CDK assets')
        self._logger.debug(f'Assets manifest file: {asset_artifacts.file}')
        self._run_command(
            f'npx cdk-assets -v publish --path {asset_artifacts.file}',
            env=environment_variables
        )

    def _get_assets_manifest(self, cloud_assembly):
        asset_artifacts = None
        for artifacts in cloud_assembly.artifacts:
            if isinstance(artifacts, aws_cdk.cx_api.AssetManifestArtifact):
                asset_artifacts = artifacts
                break
        if asset_artifacts is None:
            raise exceptions.SceptreException(f'CDK Asset manifest artifact not found')
        return asset_artifacts

    def _get_template(self, cloud_assembly: CloudAssembly) -> dict:
        return cloud_assembly.get_stack_by_name(self.STACK_LOGICAL_ID).template

    def _run_command(self, command: str, env: Dict[str, str] = None):
        # We're assuming here that the cwd is the directory to run the command from. I'm not certain
        # that will always be correct...
        result = self._subprocess_run(
            command,
            env=env,
            shell=True,
            stdout=sys.stderr,
            check=True
        )

        return result

    def _get_envs(self) -> Dict[str, str]:
        """
        Obtains the environment variables to pass to the subprocess.

        Sceptre can assume roles, profiles, etc... to connect to AWS for a given stack. This is
        very useful. However, we need that SAME connection information to carry over to CDK when we
        invoke it. The most precise way to do this is to use the same session credentials being used
        by Sceptre for other stack operations. This method obtains those credentials and sets them
        as environment variables that are passed to the subprocess and will, in turn, be used by
        SAM CLI.

        The environment variables dict created by this method will inherit all existing
        environment variables in the current environment, but the AWS connection environment
        variables will be overridden by the ones for this stack.

        Returns:
            The dictionary of environment variables.
        """
        envs = self._environment_variables.copy()
        envs.pop("AWS_PROFILE", None)
        # Set aws environment variables specific to whatever AWS configuration has been set on the
        # stack's connection manager.
        credentials: Credentials = self._connection_manager._get_session(
            self._connection_manager.profile,
            self._connection_manager.region,
            self._connection_manager.iam_role
        ).get_credentials()
        envs.update(
            AWS_ACCESS_KEY_ID=credentials.access_key,
            AWS_SECRET_ACCESS_KEY=credentials.secret_key,
            # Most AWS SDKs use AWS_DEFAULT_REGION for the region
            AWS_DEFAULT_REGION=self._connection_manager.region,
            # CDK frequently uses CDK_DEFAULT_REGION in its docs
            CDK_DEFAULT_REGION=self._connection_manager.region,
            # cdk-assets requires AWS_REGION to determine what region's STS endpoint to use
            AWS_REGION=self._connection_manager.region
        )

        # There might not be a session token, so if there isn't one, make sure it doesn't exist in
        # the envs being passed to the subprocess
        if credentials.token is None:
            envs.pop('AWS_SESSION_TOKEN', None)
        else:
            envs['AWS_SESSION_TOKEN'] = credentials.token

        return envs

    def _only_asset_is_template(self, asset_artifacts: aws_cdk.cx_api.AssetManifestArtifact):
        manifest_contents = asset_artifacts.contents
        if manifest_contents.docker_images:
            return False

        keys = list(manifest_contents.files.keys())
        expected_template = f'{self.STACK_LOGICAL_ID}.template.json'
        return keys == [expected_template]


class BootstraplessCdkBuilder(BootstrappedCdkBuilder):
    def __init__(
        self,
        logger: logging.Logger,
        connection_manager: ConnectionManager,
        synthesizer_config: dict,
        *,
        subprocess_run=subprocess.run,
        app_class=aws_cdk.App,
        environment_variables=os.environ,
        synthesizer_class=BootstraplessStackSynthesizer
    ):
        super().__init__(
            logger,
            connection_manager,
            subprocess_run=subprocess_run,
            app_class=app_class,
            environment_variables=environment_variables
        )
        self._synthesizer_config = synthesizer_config
        self._synthesizer_class = synthesizer_class

    def _synthesize(
        self,
        stack_class: Type[SceptreCdkStack],
        cdk_context: Optional[dict],
        sceptre_user_data: Any
    ) -> CloudAssembly:
        self._logger.debug(f'CDK synthesing CdkStack Class')
        self._logger.debug(f'CDK Context: {cdk_context}')
        app = self._app_class(context=cdk_context)
        try:
            synthesizer = self._synthesizer_class(**self._synthesizer_config)
        except TypeError as e:
            raise TemplateHandlerArgumentsInvalidError(
                "Error encountered attempting to instantiate the BootstraplessSynthesizer with the "
                f"specified deployment config: {e}"
            ) from e

        stack_class(app, self.STACK_LOGICAL_ID, sceptre_user_data, synthesizer=synthesizer)
        return app.synth()


class NonPythonCdkBuilder:
    def __init__(
        self,
        logger: logging.Logger,
        connection_manager: ConnectionManager,
        *,
        subprocess_run=subprocess.run,
        environment_variables=os.environ
    ):
        self._logger = logger
        self._connection_manager = connection_manager
        self._subprocess_run = subprocess_run
        self._environment_variables = environment_variables

    def build_template(self, cdk_json_path: Path, cdk_context: dict, stack_logical_id: str):
        with TemporaryDirectory() as output_dir:
            self._synthesize(cdk_json_path, output_dir, cdk_context, stack_logical_id)
            assets_file = Path(output_dir, f'{stack_logical_id}.assets.json')
            self._publish(assets_file, stack_logical_id)
            template_file = Path(output_dir, f'{stack_logical_id}.template.json')
            template = self._get_template(template_file)
            return template

    def _synthesize(self, cdk_json_path: Path, output_dir, cdk_context, stack_logical_id):
        envs = self._get_envs()
        command = self._create_command(output_dir, cdk_context, stack_logical_id)
        self._subprocess_run(
            command,
            shell=True,
            check=True,
            env=envs,
            stdout=sys.stderr,
            cwd=str(cdk_json_path.parent.resolve())
        )

    def _get_envs(self) -> Dict[str, str]:
        """
        Obtains the environment variables to pass to the subprocess.

        Sceptre can assume roles, profiles, etc... to connect to AWS for a given stack. This is
        very useful. However, we need that SAME connection information to carry over to CDK when we
        invoke it. The most precise way to do this is to use the same session credentials being used
        by Sceptre for other stack operations. This method obtains those credentials and sets them
        as environment variables that are passed to the subprocess and will, in turn, be used by
        SAM CLI.

        The environment variables dict created by this method will inherit all existing
        environment variables in the current environment, but the AWS connection environment
        variables will be overridden by the ones for this stack.

        Returns:
            The dictionary of environment variables.
        """
        envs = self._environment_variables.copy()
        envs.pop("AWS_PROFILE", None)
        # Set aws environment variables specific to whatever AWS configuration has been set on the
        # stack's connection manager.
        credentials: Credentials = self._connection_manager._get_session(
            self._connection_manager.profile,
            self._connection_manager.region,
            self._connection_manager.iam_role
        ).get_credentials()
        envs.update(
            AWS_ACCESS_KEY_ID=credentials.access_key,
            AWS_SECRET_ACCESS_KEY=credentials.secret_key,
            # Most AWS SDKs use AWS_DEFAULT_REGION for the region
            AWS_DEFAULT_REGION=self._connection_manager.region,
            # CDK frequently uses CDK_DEFAULT_REGION in its docs
            CDK_DEFAULT_REGION=self._connection_manager.region,
            # cdk-assets requires AWS_REGION to determine what region's STS endpoint to use
            AWS_REGION=self._connection_manager.region
        )

        # There might not be a session token, so if there isn't one, make sure it doesn't exist in
        # the envs being passed to the subprocess
        if credentials.token is None:
            envs.pop('AWS_SESSION_TOKEN', None)
        else:
            envs['AWS_SESSION_TOKEN'] = credentials.token

        return envs

    def _create_command(self, output_dir: str, cdk_context: Dict[str, str], stack_logical_id):
        command = f'npx cdk synth {stack_logical_id} -o {output_dir} '
        for key, value in cdk_context.items():
            command += f'{key}={value} '

        return command

    def _publish(self, assets_filepath: Path, stack_logical_id: str):
        assets_manifest = self._get_assets_manifest(assets_filepath)
        if self._only_asset_is_template(assets_manifest, stack_logical_id):
            # Sceptre already has a mechanism to upload the template if configured. We don't
            # need to deploy assets if the only asset is the template
            self._logger.debug("Only asset is template; Skipping asset upload.")
            return

        environment_variables = self._get_envs()
        self._logger.info(f'Publishing CDK assets')
        self._logger.debug(f'Assets manifest file: {assets_filepath}')
        self._subprocess_run(
            f'npx cdk-assets -v publish --path {assets_filepath}',
            shell=True,
            check=True,
            env=environment_variables,
            stdout=sys.stderr,
        )

    def _get_assets_manifest(self, assets_filepath: Path):
        if not assets_filepath.exists():
            raise exceptions.SceptreException(f'CDK Asset manifest artifact not found')

        with assets_filepath.open(mode='r') as f:
            assets_dict = json.load(f)
        return assets_dict

    def _only_asset_is_template(self, assets_dict: dict, stack_logical_id: str) -> bool:
        if assets_dict.get('dockerImages', {}):
            return False

        keys = list(assets_dict.get('files', {}).keys())
        expected_template = f'{stack_logical_id}.template.json'
        return keys == [expected_template]

    def _get_template(self, template_path: Path):
        with template_path.open(mode='r') as f:
            return json.load(f)
