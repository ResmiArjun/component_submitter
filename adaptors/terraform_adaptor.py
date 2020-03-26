import filecmp
import os
import copy
import logging
import time
import shutil

import requests
import docker
import ruamel.yaml as yaml
import json
import jinja2

import utils
from abstracts import base_adaptor as abco
from abstracts.exceptions import AdaptorCritical
from toscaparser.tosca_template import ToscaTemplate

logger = logging.getLogger("adaptor." + __name__)

# Append to Terraform commands to create output in the Terraform container
LOG_SUFFIX = (
    " | while IFS= read -r line;"
    ' do printf "%s %s\n" "$(date "+[%Y-%m-%d %H:%M:%S]")" "$line";'
    " done | tee /proc/1/fd/1"
)
SUPPORTED_CLOUDS = ("ec2", "nova", "azure", "gce")


class TerraformDict(dict):
    def __init__(self):
        super().__init__()
        self["//"] = "This file has been generated by the MiCADO Terraform Adaptor"
        self.resource = []
        self.data = []
        self.provider = {}
        self.variable = {}
        self.ip_list = {}
        self.tfvars = {}

    def add_provider(self, name, properties):
        self.setdefault("provider", {})
        if name not in self["provider"]:
            self["provider"][name] = properties
        self.provider = self["provider"]

    def add_variable(self, name, properties):
        self.setdefault("variable", {})
        if name not in self["variable"]:
            self["variable"][name] = properties
        self.variable = self["variable"]

    def add_output(self, name, value):
        self.setdefault("output", {})
        if name not in self["output"]:
            self["output"][name] = {}
            self["output"][name]["value"] = value
        self.output = self["output"]

    def add_resource(self, name, resource):
        self.setdefault("resource", {})
        self["resource"].setdefault(name, [])
        if resource not in self["resource"][name]:
            self["resource"][name].append(resource)
        self.resource = self["resource"]

    def add_data(self, name, data):
        self.setdefault("data", {})
        self["data"].setdefault(name, [])
        if data not in self["data"][name]:
            self["data"][name].append(data)
        self.data = self["data"]

    def add_instance_variable(self, name, value):
        self.add_variable(name, {})
        node_list = []
        for i in range(1, value + 1):
            node_list.append(str(i))
        self.tfvars[name] = node_list

    def update_instance_vars(self, old_node_list):
        new_counts = {}
        for node_name, new_node_list in self.tfvars.items():
            if node_name in old_node_list:
                new_counts[node_name] = old_node_list[node_name]
            else:
                new_counts[node_name] = new_node_list
        self.tfvars = new_counts

    def dump_json(self, path_to_tf, path_to_vars):
        utils.dump_json(self, path_to_tf)
        utils.dump_json(self.tfvars, path_to_vars)


class TerraformAdaptor(abco.Adaptor):
    def __init__(self, adaptor_id, config, dryrun, validate=False, template=None):
        """
        Constructor method of the Adaptor
        """
        super().__init__()
        if template and not isinstance(template, ToscaTemplate):
            raise AdaptorCritical("Template is not a valid TOSCAParser object")
        self.status = "init"
        self.dryrun = dryrun
        self.volume = config["volume"]
        self.validate = validate
        self.node_name = ""
        self.min_instances = 1
        self.max_instances = 1
        self.app_name = adaptor_id
        self.template = template

        self.terra_path = "/var/lib/micado/terraform/submitter/"

        self.tf_file = "{}{}.tf.json".format(self.volume, self.app_name)
        self.tf_file_tmp = "{}{}.tf.json.tmp".format(self.volume, self.app_name)
        self.vars_file = "{}terraform.tfvars.json".format(self.volume)
        self.vars_file_tmp = "{}terraform.tfvars.json.tmp".format(self.volume)
        self.account_file = "{}accounts.json".format(self.volume)

        self.cloud_init_template = "./system/cloud_init_worker_tf.yaml"
        self.auth_data_file = "/var/lib/submitter/system/auth_data.yaml"
        self.auth_gce = "/var/lib/submitter/system/gce_auth.json"
        self.master_cert = "/var/lib/submitter/system/master.pem"

        self.tf_json = TerraformDict()

        self.created = False
        self.terraform = None
        self.cloud_inits = set()
        if not self.dryrun:
            self._init_docker()

        logger.info("Terraform adaptor initialised")

    def translate(self, update=False):
        """
        Translate the self.tpl subset to Terraform node infrastructure format
        This fuction creates a mapping between TOSCA and Terraform template descriptor.
        """
        logger.info("Starting Terraform Translation")
        self.status = "translating..."
        self.tf_json = TerraformDict()

        for node in self.template.nodetemplates:

            if "_" in node.name:
                raise AdaptorCritical(
                    "Underscores in node {} not allowed".format(node.name)
                )

            self.node_name = node.name
            node = copy.deepcopy(node)
            tf_interface = self._get_terraform_interface(node)
            if not tf_interface:
                continue

            self._get_policies(node)
            tf_options = utils.resolve_get_property(tf_interface.get("create"))
            properties = self._get_properties_values(node)
            properties.update(tf_options)

            context = properties.get("context")
            cloud_init = self._node_data_get_context_section(context)
            self.cloud_inits.add(cloud_init)

            cloud_type = utils.get_cloud_type(node, SUPPORTED_CLOUDS)

            if cloud_type == "ec2":
                logger.debug("EC2 resource detected")
                aws_properties = self._consolidate_ec2_properties(properties)
                self._add_terraform_aws(aws_properties)
            elif cloud_type == "nova":
                logger.debug("Nova resource detected")
                self._add_terraform_nova(properties)
            elif cloud_type == "azure":
                logger.debug("Azure resource detected")
                self._add_terraform_azure(properties)
            elif cloud_type == "gce":
                logger.debug("GCE resource detected")
                self._add_terraform_gce(properties)

        if not self.tf_json.provider:
            logger.info("No nodes to orchestrate with Terraform. Skipping...")
            self.status = "Skipped"
            return

        if update:
            logger.debug("Creating temp files")
            old_instance_vars = utils.load_json(self.vars_file)
            self.tf_json.update_instance_vars(old_instance_vars)
            self.tf_json.dump_json(self.tf_file_tmp, self.vars_file_tmp)

        elif not self.validate:
            self.tf_json.dump_json(self.tf_file, self.vars_file)
            self._rename_tmp_cloudinits()

        self.status = "Translated"

    def execute(self, update=False):
        """
        Initialize terraform execution environment and execute
        """
        logger.info("Starting Terraform execution {}".format(self.app_name))
        self.status = "executing"
        if self._skip_check():
            return

        lock_timeout = 0
        if not update:
            logger.debug("Terraform initialization starting...")
            self._terraform_init()
        else:
            lock_timeout = 300

        logger.debug("Terraform apply starting...")
        self._terraform_apply(lock_timeout)
        logger.info("Terraform executed")
        self.status = "executed"

    def undeploy(self):
        """
        Undeploy Terraform infrastructure
        """
        self.status = "undeploying"
        logger.info("Undeploying {} infrastructure".format(self.app_name))
        if self._skip_check():
            return

        logger.debug("Starting terraform destroy...")
        self._terraform_destroy()

    def cleanup(self):
        """
        Remove the generated files under "files/output_configs/"
        """
        logger.info("Cleanup config for ID {}".format(self.app_name))
        if not self._config_file_exists():
            logger.info("Terraform plan not found, skipping cleanup")
            self.status = "Skipped"
            return

        self._remove_cloud_inits()
        files_to_clean = [
            self.tf_file,
            self.vars_file,
            self.account_file,
            self.volume + "terraform.tfstate",
            self.volume + "terraform.tfstate.backup",
        ]
        for file in files_to_clean:
            try:
                os.remove(file)
            except OSError:
                pass

    def update(self):
        """
        Check that if it's any change in the node definition or in the cloud-init file.
        If the node definition changed then rerun the build process. If the infrastructure definition
        changed first undeploy the infrastructure and rebuild it with the modified parameter.
        """
        self.status = "updating"
        self.min_instances = 1
        self.max_instances = 1
        logger.info("Updating the infrastructure {}".format(self.app_name))
        self.translate(update=True)

        if not self.tf_json.provider and self._config_file_exists():
            logger.debug("All Terraform nodes removed from ADT. Undeploying...")
            self._remove_tmp_files()
            self.undeploy()
            self.cleanup()
            self.status = "Updated (undeployed)"

        elif not self.tf_json.provider:
            logger.debug("No Terraform nodes added to ADT")
            self._remove_tmp_files()
            self.status = "Skipped"

        elif self._differentiate(self.tf_file, self.tf_file_tmp):
            logger.debug("Terraform file changed, replacing and executing...")
            self._rename_all_tmp_files()
            self.execute(True)
            self.status = "Updated Terraform file"

        elif self._differentiate_cloud_inits():
            logger.debug("Cloud-init file(s) changed, replacing old executing")
            self._rename_all_tmp_files()
            self.execute(True)
            self.status = "Updated cloud_init files"

        else:
            logger.info("There are no changes in the Terraform files")
            self._remove_tmp_files()
            self.status = "Updated (nothing to update)"

    def _get_terraform_interface(self, node):
        """
        Return tosca interfaces for the node
        """
        interfaces = utils.get_lifecycle(node, "Terraform")
        if not interfaces:
            logger.debug("No interface for Terraform in {}".format(node.name))
        return interfaces

    def _node_data_get_context_section(self, context):
        """
        Create the cloud-init config file
        """
        if not context:
            logger.debug("The adaptor will use a default cloud-config")
            node_init = self._get_cloud_init(None, False, False)

        elif context.get("append"):
            if not context.get("cloud_config"):
                logger.error(
                    "You set append properties but you do not have cloud_config. Please check it again!"
                )
                raise AdaptorCritical(
                    "You set append properties but you don't have cloud_config. Please check it again!"
                )
            else:
                logger.debug("Append the TOSCA cloud-config to the default config")
                node_init = self._get_cloud_init(context["cloud_config"], True, False)

        else:
            if not context.get("cloud_config"):
                logger.debug("The adaptor will use a default cloud-config")
                node_init = self._get_cloud_init(None, False, False)
            else:
                logger.debug("The adaptor will use the TOSCA cloud-config")
                node_init = self._get_cloud_init(context["cloud_config"], False, True)

        cloud_init_file_name = "{}-cloud-init.yaml".format(self.node_name)
        cloud_init_path = "{}{}".format(self.volume, cloud_init_file_name)
        cloud_init_path_tmp = "{}.tmp".format(cloud_init_path)

        utils.dump_order_yaml(node_init, cloud_init_path_tmp)
        return cloud_init_path

    def _consolidate_ec2_properties(self, properties):
        """
        Return consolidated & renamed EC2 property keys
        """
        aws_properties = {}
        aws_properties["region"] = properties["region_name"]
        aws_properties["ami"] = properties["image_id"]
        aws_properties["instance_type"] = properties["instance_type"]
        if properties.get("key_name"):
            aws_properties["key_name"] = properties["key_name"]
        if properties.get("security_group_ids"):
            security_groups = properties["security_group_ids"]
            aws_properties["vpc_security_group_ids"] = security_groups

        return aws_properties

    def _get_cloud_init(self, tosca_cloud_config, append, override):
        """
        Get cloud-config from MiCADO cloud-init template
        """
        yaml.default_flow_style = False
        default_cloud_config = {}
        with open(self.master_cert, "r") as p:
            master_file = p.read()
        try:
            with open(self.cloud_init_template, "r") as f:
                template = jinja2.Template(f.read())
                rendered = template.render(
                    worker_name=self.node_name, master_pem=master_file
                )
                default_cloud_config = yaml.round_trip_load(
                    rendered, preserve_quotes=True
                )
        except OSError as e:
            logger.error(e)
        if override:
            return yaml.round_trip_load(tosca_cloud_config, preserve_quotes=True)
        if tosca_cloud_config is not None:
            tosca_cloud_config = yaml.round_trip_load(
                tosca_cloud_config, preserve_quotes=True
            )
        if append:
            for x in default_cloud_config:
                for y in tosca_cloud_config:
                    if x == y:
                        for z in tosca_cloud_config[y]:
                            # Insert anywhere before 'join kubernetes'
                            default_cloud_config[x].insert(29, z)
            return default_cloud_config
        else:
            return default_cloud_config

    def _get_properties_values(self, node):
        """ Get host properties """
        return {x: y.value for x, y in node.get_properties().items()}

    def _get_policies(self, node):
        """ Get the TOSCA policies """
        self.min_instances = 1
        self.max_instances = 1
        if "scalable" in node.entity_tpl.get("capabilities", {}):
            scalable = node.get_capabilities()["scalable"]
            self.min_instances = scalable.get_property_value("min_instances")
            self.max_instances = scalable.get_property_value("max_instances")
            return
        for policy in self.template.policies:
            for target in policy.targets_list:
                if self.node_name == target.name:
                    logger.debug("policy target match for compute node")
                    properties = self._get_properties_values(policy)
                    self.min_instances = properties["min_instances"]
                    self.max_instances = properties["max_instances"]

    def _differentiate(self, path, tmp_path):
        """ Compare two files """
        return not filecmp.cmp(path, tmp_path)

    def _differentiate_cloud_inits(self):
        """ Compare cloud inits """
        for cloud_init in self.cloud_inits:
            cloud_init_tmp = "{}.tmp".format(cloud_init)
            if os.path.exists(cloud_init) and self._differentiate(
                cloud_init, cloud_init_tmp
            ):
                return True

    def _add_terraform_aws(self, properties):
        """ Add Terraform template for AWS to JSON"""

        # Get the credentials info
        credential = self._get_credential_info("ec2")

        # Check regions match
        region = properties.pop("region")
        aws_region = self.tf_json.provider.get("aws", {}).get("region")
        if aws_region and aws_region != region:
            raise AdaptorCritical("Multiple different AWS regions is unsupported")

        # Add the provider
        aws_provider = {
            "version": "~> 2.54",
            "region": region,
            "access_key": credential["accesskey"],
            "secret_key": credential["secretkey"],
        }
        endpoint = properties.pop("endpoint", None)
        if endpoint:
            aws_provider.setdefault("endpoints", {})["ec2"] = endpoint
        self.tf_json.add_provider("aws", aws_provider)

        instance_name = self.node_name
        cloud_init_file_name = "{}-cloud-init.yaml".format(instance_name)

        # Add the count variable
        self.tf_json.add_instance_variable(instance_name, self.min_instances)

        # Add the resource
        aws_instance = {
            instance_name: {
                **properties,
                "user_data": '${file("${path.module}/%s")}' % cloud_init_file_name,
                "instance_initiated_shutdown_behavior": "terminate",
                "for_each": "${toset(var.%s)}" % instance_name,
            }
        }
        # Add the name tag if no tags present
        aws_instance[instance_name].setdefault("tags", {"Name": instance_name})
        aws_instance[instance_name]["tags"]["Name"] += "${each.key}"
        self.tf_json.add_resource("aws_instance", aws_instance)

        # Add the IP output
        ip_output = {
            "private_ips": "${[for i in aws_instance.%s : i.private_ip]}"
            % instance_name,
            "public_ips": "${[for i in aws_instance.%s : i.public_ip]}" % instance_name,
        }
        self.tf_json.add_output(instance_name, ip_output)

    def _add_terraform_nova(self, properties):
        """ Write Terraform template files for openstack in JSON"""

        def get_provider():
            return {
                "version": "~> 1.26",
                "auth_url": auth_url,
                "tenant_id": tenant_id,
                "user_name": credential["username"],
                "password": credential["password"],
            }

        def get_virtual_machine():
            return {
                instance_name: {
                    "name": "%s${each.key}" % instance_name,
                    "image_id": image_id,
                    "flavor_id": flavor_id,
                    "key_pair": key_pair,
                    "security_groups": security_groups,
                    "user_data": '${file("${path.module}/%s")}' % cloud_init_file_name,
                    "for_each": "${toset(var.%s)}" % instance_name,
                    "network": {"name": network_name, "uuid": network_id,},
                }
            }

        instance_name = self.node_name
        self.tf_json.add_instance_variable(instance_name, self.min_instances)

        credential = self._get_credential_info("nova")
        auth_url = properties.get("auth_url") or properties.get("endpoint")
        tenant_id = properties["project_id"]
        self.tf_json.add_provider("openstack", get_provider())

        image_id = properties["image_id"]
        flavor_id = properties.get("flavor_name") or properties.get("flavor_id")
        network_name = properties["network_name"]
        network_id = properties["network_id"]
        key_pair = properties["key_name"]
        security_groups = properties["security_groups"]
        cloud_init_file_name = "{}-cloud-init.yaml".format(instance_name)
        self.tf_json.add_resource(
            "openstack_compute_instance_v2", get_virtual_machine()
        )

    def _add_terraform_azure(self, properties):
        """ Write Terraform template files for Azure in JSON"""

        def get_provider(use_msi):
            provider = {"version": "~> 2.2"}
            provider["subscription_id"] = credential["subscription_id"]
            provider["features"] = {}
            if use_msi:
                provider["use_msi"] = "true"
            else:
                provider.update(
                    {
                        "tenant_id": credential["tenant_id"],
                        "client_id": credential["client_id"],
                        "client_secret": credential["client_secret"],
                    }
                )
            return provider

        def get_resource_group():
            return {resource_group_name: {"name": resource_group_name}}

        def get_virtual_network():
            return {
                virtual_network_name: {
                    "name": virtual_network_name,
                    "resource_group_name": "${data.azurerm_resource_group.%s.name}"
                    % resource_group_name,
                }
            }

        def get_subnet():
            return {
                subnet_name: {
                    "name": subnet_name,
                    "resource_group_name": "${data.azurerm_resource_group.%s.name}"
                    % resource_group_name,
                    "virtual_network_name": "${data.azurerm_virtual_network.%s.name}"
                    % virtual_network_name,
                }
            }

        def get_network_security_group():
            return {
                network_security_group_name: {
                    "name": network_security_group_name,
                    "resource_group_name": "${data.azurerm_resource_group.%s.name}"
                    % resource_group_name,
                }
            }

        def get_public_ip():
            return {
                public_ip: {
                    "name": "%s${each.key}" % public_ip,
                    "location": "${data.azurerm_resource_group.%s.location}"
                    % resource_group_name,
                    "resource_group_name": "${data.azurerm_resource_group.%s.name}"
                    % resource_group_name,
                    "allocation_method": "Static",
                    "for_each": "${toset(var.%s)}" % instance_name,
                }
            }

        def get_network_interface():
            return {
                network_interface_name: {
                    "name": "%s${each.key}" % network_interface_name,
                    "location": "${data.azurerm_resource_group.%s.location}"
                    % resource_group_name,
                    "resource_group_name": "${data.azurerm_resource_group.%s.name}"
                    % resource_group_name,
                    "for_each": "${toset(var.%s)}" % instance_name,
                    "ip_configuration": {
                        "name": "%s${each.key}" % nic_config_name,
                        "subnet_id": "${data.azurerm_subnet.%s.id}" % subnet_name,
                        "private_ip_address_allocation": "Dynamic",
                    },
                }
            }

        def get_network_security_association():
            return {
                network_security_assoc_name: {
                    "for_each": "${toset(var.%s)}" % instance_name,
                    "network_interface_id": "${azurerm_network_interface.%s[each.key].id}"
                    % network_interface_name,
                    "network_security_group_id": "${data.azurerm_network_security_group.%s.id}"
                    % network_security_group_name,
                }
            }

        def get_lin_virtual_machine():
            return {
                instance_name: {
                    "name": "%s${each.key}" % instance_name,
                    "location": "${data.azurerm_resource_group.%s.location}"
                    % resource_group_name,
                    "resource_group_name": "${data.azurerm_resource_group.%s.name}"
                    % resource_group_name,
                    "network_interface_ids": [
                        "${azurerm_network_interface.%s[each.key].id}"
                        % network_interface_name
                    ],
                    "size": size,
                    "admin_username": admin_user or "ubuntu",
                    "admin_ssh_key": {
                        "username": admin_user or "ubuntu",
                        "public_key": public_key,
                    },
                    "for_each": "${toset(var.%s)}" % instance_name,
                    "os_disk": {
                        "caching": "ReadWrite",
                        "storage_account_type": "Standard_LRS",
                    },
                    "source_image_reference": {
                        "publisher": "Canonical",
                        "offer": "UbuntuServer",
                        "sku": image_sku or "18.04-LTS",
                        "version": "latest",
                    },
                    "custom_data": '${filebase64("${path.module}/%s")}'
                    % cloud_init_file_name,
                }
            }

        def get_win_virtual_machine():
            return {
                instance_name: {
                    "name": "%s${each.key}" % instance_name,
                    "location": "${data.azurerm_resource_group.%s.location}"
                    % resource_group_name,
                    "resource_group_name": "${data.azurerm_resource_group.%s.name}"
                    % resource_group_name,
                    "size": size,
                    "admin_username": admin_user or "windows",
                    "admin_password": admin_pass or "Xyzabc123@",
                    "network_interface_ids": [
                        "${azurerm_network_interface.%s[each.key].id}"
                        % network_interface_name
                    ],
                    "for_each": "${toset(var.%s)}" % instance_name,
                    "os_disk": {
                        "caching": "ReadWrite",
                        "storage_account_type": "Standard_LRS",
                    },
                    "source_image_reference": {
                        "publisher": "MicrosoftWindowsServer",
                        "offer": "WindowsServer",
                        "sku": image_sku or "2016-Datacenter",
                        "version": "latest",
                    },
                }
            }

        def get_ip_output():
            return {
                "private_ips": "${[for i in azurerm_network_interface.%s : i.private_ip_address]}"
                % network_interface_name
            }

        # Begin building the JSON
        instance_name = self.node_name

        credential = self._get_credential_info("azure")

        # Check whether to authenticate with a Managed Service Identity
        use_msi = any(
            [
                not credential.get("client_secret"),
                credential.get("use_msi", "").lower() == "true",
                properties.pop("use_msi", "").lower() == "true",
            ]
        )
        self.tf_json.add_provider("azurerm", get_provider(use_msi))

        self.tf_json.add_instance_variable(instance_name, self.min_instances)

        resource_group_name = properties["resource_group"]
        self.tf_json.add_data("azurerm_resource_group", get_resource_group())

        virtual_network_name = properties["virtual_network"]
        self.tf_json.add_data("azurerm_virtual_network", get_virtual_network())

        subnet_name = properties["subnet"]
        self.tf_json.add_data("azurerm_subnet", get_subnet())

        network_security_group_name = properties["network_security_group"]
        self.tf_json.add_data(
            "azurerm_network_security_group", get_network_security_group()
        )

        nic_config_name = "{}-nic-config".format(instance_name)
        network_interface_name = "{}-nic".format(instance_name)
        network_interface = get_network_interface()
        if properties.get("public_ip"):
            public_ip = "{}-ip".format(instance_name)
            network_interface[network_interface_name]["ip_configuration"][
                "public_ip_address_id"
            ] = ("${azurerm_public_ip.%s[each.key].id}" % public_ip)
            self.tf_json.add_resource("azurerm_public_ip", get_public_ip())

        self.tf_json.add_resource("azurerm_network_interface", network_interface)

        network_security_assoc_name = instance_name + "security-association"
        self.tf_json.add_resource(
            "azurerm_network_interface_security_group_association",
            get_network_security_association(),
        )

        size = properties["size"]
        image_sku = properties.get("image_sku")
        source_image_id = properties.get("source_image_id")
        admin_user = properties.get("admin_username")
        admin_pass = properties.get("admin_password")
        cloud_init_file_name = "{}-cloud-init.yaml".format(instance_name)

        if properties.get("public_key"):
            public_key = properties.get("public_key")
            virtual_machine = get_lin_virtual_machine()
            virtual_machine_os = "azurerm_linux_virtual_machine"
        else:
            virtual_machine = get_win_virtual_machine()
            virtual_machine_os = "azurerm_windows_virtual_machine"
            if properties.get("context"):
                virtual_machine[instance_name]["custom_data"] = (
                    '${filebase64("${path.module}/%s")}' % cloud_init_file_name
                )

        if source_image_id:
            virtual_machine[instance_name].pop("source_image_reference")
            virtual_machine[instance_name]["source_image_id"] = source_image_id

        self.tf_json.add_resource(virtual_machine_os, virtual_machine)
        self.tf_json.add_output(instance_name, get_ip_output())

    def _add_terraform_gce(self, properties):
        """ Write Terraform template files for GCE in JSON"""

        def get_provider():
            return {
                "version": "~> 3.14",
                "credentials": '${file("accounts.json")}',
                "project": project,
                "region": region,
            }

        def get_virtual_machine():
            return {
                instance_name: {
                    "name": "%s${each.key}" % instance_name,
                    "machine_type": machine_type,
                    "zone": zone,
                    "for_each": "${toset(var.%s)}" % instance_name,
                    "boot_disk": {"initialize_params": {"image": image,},},
                    "network_interface": {"network": network, "access_config": {},},
                    "metadata": {
                        "ssh-keys": "ubuntu:%s" % ssh_keys,
                        "user-data": '${file("${path.module}/%s")}'
                        % cloud_init_file_name,
                    },
                }
            }

        instance_name = self.node_name

        self.tf_json.add_instance_variable(instance_name, self.min_instances)

        shutil.copyfile(self.auth_gce, self.account_file)

        project = properties["project"]
        region = properties["region"]
        self.tf_json.add_provider("google", get_provider())

        image = properties["image"]
        network = properties["network"]
        machine_type = properties["machine_type"]
        zone = properties["zone"]
        ssh_keys = properties["ssh-keys"]
        cloud_init_file_name = "{}-cloud-init.yaml".format(instance_name)
        self.tf_json.add_resource("google_compute_instance", get_virtual_machine())

    def _config_file_exists(self):
        """ Check if config file was generated during translation """
        return os.path.exists(self.tf_file)

    def _skip_check(self):
        if not self._config_file_exists():
            logger.info("No config generated, skipping {} step...".format(self.status))
            self.status = "Skipped"
            return True
        elif self.dryrun:
            logger.info("DRY-RUN: Terraform {} in dry-run mode...".format(self.status))
            self.status = "DRY-RUN Deployment"
            return True
        else:
            return False

    def _remove_tmp_files(self):
        """ Remove tmp files generated by the update step """
        try:
            os.remove(self.tf_file_tmp)
        except OSError:
            pass

        try:
            os.remove(self.vars_file_tmp)
        except OSError:
            pass

        for cloud_init in self.cloud_inits:
            cloud_init_tmp = cloud_init + ".tmp"
            try:
                os.remove(cloud_init_tmp)
            except OSError:
                pass

    def _remove_cloud_inits(self):
        """ Remove cloud_init files on undeploy """
        for file in os.listdir(self.volume):
            if "cloud-init.yaml" in file:
                try:
                    os.remove(self.volume + file)
                except OSError:
                    pass

    def _rename_tmp_cloudinits(self):
        """ Rename temporary cloud_init files """
        for cloud_init in self.cloud_inits:
            cloud_init_tmp = cloud_init + ".tmp"
            os.rename(cloud_init_tmp, cloud_init)

    def _rename_all_tmp_files(self):
        """ Rename all temporary files """
        os.rename(self.tf_file_tmp, self.tf_file)
        os.rename(self.vars_file_tmp, self.vars_file)
        self._rename_tmp_cloudinits()

    def _get_credential_info(self, provider):
        """ Return credential info from file """
        with open(self.auth_data_file, "r") as stream:
            temp = yaml.safe_load(stream)
        resources = temp.get("resource", {})
        for resource in resources:
            if resource.get("type") == provider:
                return resource.get("auth_data")

    def _init_docker(self):
        """ Initialize docker and get Terraform container """
        client = docker.from_env()
        i = 0

        while not self.created and i < 5:
            try:
                self.terraform = client.containers.list(
                    filters={"label": "io.kubernetes.container.name=terraform"}
                )[0]
                self.created = True
            except Exception as e:
                i += 1
                logger.error("{0}. Try {1} of 5.".format(str(e), i))
                time.sleep(5)

    def _terraform_exec(self, command, lock_timeout=0):
        """ Execute the command in the terraform container """
        if not self.created:
            logger.error("Could not attach to Terraform container!")
            raise AdaptorCritical("Could not attach to Terraform container!")
        while True:
            exit_code, out = self.terraform.exec_run(
                command, workdir="{}".format(self.terra_path),
            )
            if exit_code > 0:
                logger.error("Terraform exec failed {}".format(out))
                raise AdaptorCritical("Terraform exec failed {}".format(out))
            elif lock_timeout > 0 and "Error locking state" in str(out):
                time.sleep(5)
                lock_timeout -= 5
                logger.debug("Waiting for lock, {}s until timeout".format(lock_timeout))
            else:
                break
        return str(out)

    def _terraform_init(self):
        """ Run terraform init in the container """
        command = ["sh", "-c", "terraform init -no-color" + LOG_SUFFIX]
        exec_output = self._terraform_exec(command)
        if "successfully initialized" in exec_output:
            logger.debug("Terraform initialization has been successful")
        else:
            raise AdaptorCritical("Terraform init failed: {}".format(exec_output))

    def _terraform_apply(self, lock_timeout):
        """ Run terraform apply in the container """
        command = ["sh", "-c", "terraform apply -auto-approve -no-color" + LOG_SUFFIX]
        exec_output = self._terraform_exec(command, lock_timeout)
        if "Apply complete" in exec_output:
            logger.debug("Terraform apply has been successful")
        else:
            raise AdaptorCritical("Terraform apply failed: {}".format(exec_output))

    def _terraform_destroy(self):
        """ Run terraform destroy in the container """
        command = [
            "sh",
            "-c",
            "terraform destroy -auto-approve -no-color" + LOG_SUFFIX,
        ]
        exec_output = self._terraform_exec(command, lock_timeout=600)
        if "Destroy complete" in exec_output:
            logger.debug("Terraform destroy successful...")
            self.status = "undeployed"
        else:
            raise AdaptorCritical("Undeploy failed: {}".format(exec_output))
