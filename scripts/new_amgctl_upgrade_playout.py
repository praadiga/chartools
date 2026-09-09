import yaml
import subprocess
import yaml
import os
import time
import re
import argparse
import sys
import csv
import random
import string
import datetime
from datetime import timedelta
import uuid
import urllib.parse
import requests
import json

BLUE_START = "\033[34m"
BLUE_END = "\033[0m"

GREEN_START = "\033[32m"
GREEN_END = "\033[0m"

RED_START = "\033[31m"
RED_END = "\033[0m"

YELLOW_START = "\033[33m"
YELLOW_END = "\033[0m"


def copy_amgctl_from_player(cp_namespace, cp_podname, cp_container, api_version):
    dir2="/home/amagi/lh_upgrade/k8s_player"
    command_to_download_amgctl = f"""
    kubectl exec -n {cp_namespace} --kubeconfig={dir2}/kubeconfig.yaml {cp_podname} -c {cp_container} -- /bin/sh -c "aws s3 cp s3://iota-non-prod-artifacts/ieg-core_services/{api_version}/binaries/amgctl_Linux_x86_64.tar.gz /mnt/"
    """

    aws_result = subprocess.run(command_to_download_amgctl, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    print(f"Downloading amgctl_version: {aws_result}")


    command_to_cp_amgctl = f"""
    kubectl cp -n {cp_namespace} --kubeconfig={dir2}/kubeconfig.yaml {cp_namespace}/{cp_podname}:/mnt/amgctl_Linux_x86_64.tar.gz amgctl_Linux_x86_64.tar.gz -c {cp_container}
    """
    res_cp_amgctl = subprocess.run(command_to_cp_amgctl, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    print(f"Copying amgctl to local: {res_cp_amgctl}")

def copy_amgctl_using_aws_profile(api_version):
    command_to_download_amgctl_using_aws_profile = f"""
    aws s3 cp s3://iota-non-prod-artifacts/ieg-core_services/{api_version}/binaries/amgctl_Linux_x86_64.tar.gz .
    """

    down_result = subprocess.run(command_to_download_amgctl_using_aws_profile, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    
    print(f"Downloading amgctl_version:\n {down_result}")

def install_latest_amgctl(cp_namespace, cp_podname, cp_container, api_version_download, sudo_pwd):
    command_to_check_playout = f"""
    ./amgctl version
    """
    check_playout_res = subprocess.run(command_to_check_playout, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    
    output = check_playout_res.stdout + check_playout_res.stderr

    print(f"amgctl version output:{output}")

    api_match = re.search(r"API Version:\s*([0-9.]+)", output)
    cli_match = re.search(r"CLI Version:\s*([0-9.]+)", output)
    # If not found, try fatal mismatch format
    if not api_match:
        api_match = re.search(r"api_version=([0-9.]+)", output)
    if not cli_match:
        cli_match = re.search(r"cli_version=([0-9.]+)", output)
    if api_match:
        api_version = api_match.group(1) if api_match else None
    else:
        api_version = None
        print("API version not found")
    if cli_match:
        cli_version = cli_match.group(1) if cli_match else None
    else:
        cli_version = None
        print("CLI version not found")

    print("API Version:", api_version)
    print("CLI Version:", cli_version)
    print(check_playout_res)
    #if check_playout_res.stderr != "":
    #    count = 1
    #    for line in str(check_playout_res.stderr).split("\n"):
    #        if "FATAL" in line:
    #            print(f"{RED_START}{line} {RED_END}")
    #            api_version = str(line).split(" ")[-2]
    #            api_version = api_version.split("=")[1]
    #            print(api_version)
    #            cli_version = str(line).split(" ")[-1].split("=")[1]
    #            print(f"{RED_START}FATAL: amgctl cli version mismatch{RED_END}\n{GREEN_START}Changing amgctl cli version {GREEN_END} {YELLOW_START}{cli_version}{YELLOW_END}{GREEN_START} to api version{GREEN_END} {YELLOW_START}{api_version}{YELLOW_END}")
    
    '''
    for line in str(check_playout_res).split("x1b"):
        if "FATAL" in line:
            print(line)
            print("Changing amgctl cli version to api version")
            return
    '''
    if api_version != cli_version or cli_version == None or check_playout_res.stderr != "":
        #copy_amgctl_from_player(cp_namespace, cp_podname, cp_container, api_version)
        if cli_version == None:
            api_version = api_version_download
        copy_amgctl_using_aws_profile(api_version)

        command_to_extract = f"""
        tar -xvzf amgctl_Linux_x86_64.tar.gz
        """
        res_cp_ext = subprocess.run(command_to_extract, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

        print(f"Copying amgctl to local: {res_cp_ext}")

        #cmd_mkdir_bin = """
        #mkdir -p /var/jenkins_home/bin
        #"""
        #res_cp_inst = subprocess.run(cmd_mkdir_bin, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

        #command_to_install = f"""
        #mv amgctl /var/jenkins_home/bin/
        #"""
        #res_cp_inst = subprocess.run(command_to_install, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

        #print(f"Copying amgctl to local: {res_cp_inst}")

        #command_to_add_path = f"""
        #export PATH=/var/jenkins_home/bin:$PATH
        #"""
        #res_path = subprocess.run(command_to_add_path, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

        check_amgctl_version_cmd = f"""
        #export PATH=/var/jenkins_home/bin:$PATH
        ./amgctl --version
        """
        res_amgctl_version = subprocess.run(check_amgctl_version_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

        print(f"installed_amgctl_version::::: {res_amgctl_version}")
        amgctl_version_found = res_amgctl_version.stdout.split(" ")[2]

        print(res_amgctl_version.stderr)

        if res_amgctl_version.stderr != "":
             print(f"Cannot install amgctl version cause:\n {res_amgctl_version.stderr}")

        if str(amgctl_version_found).strip() == str(api_version).strip():
             print(f"Successfully installed amgctl version to: {amgctl_version_found}")
        if cli_version == None:
            print("cli version is still {cli_version} Rerunning the same function again")
            install_latest_amgctl(cp_namespace, cp_podname, cp_container, api_version_download, sudo_pwd)
    else:
        print(f"Api_version({api_version}) == Cli_version({cli_version}), continuing with without downloading amgctl version...")

def update_yaml_key(file_path, key_path, new_value):
    if isinstance(new_value, str):
        if new_value == 'False':
            new_value = False
        elif new_value == 'True':
            new_value = True

    # Load the YAML file
    with open(file_path, 'r') as file:
        data = yaml.safe_load(file) or {}  # Ensure data is a dictionary even if the file is empty

    # Split the key path into individual keys
    keys = key_path.split('.')
    config = data

    # Traverse or create nested dictionaries
    for key in keys[:-1]:
        if key not in config or not isinstance(config[key], dict):
            config[key] = {}  # Create missing dictionaries
        config = config[key]

    # Set the value for the final key
    config[keys[-1]] = new_value

    # Write the updated YAML back to the file
    with open(file_path, 'w') as file:
        yaml.safe_dump(data, file, default_flow_style=False)

def create_n_cp_prefill_playout_coreservice(yml_file_name, topology_folder, workspace, addon, appinfra, appstack, plsh, cp_release_first3_char):
    print(f"creating topology for {cp_release_first3_char}")
    data = {}
    if int(cp_release_first3_char) >= 417:    
        data = {
            "cloud_kind": "aws",
            "cloud_name": "iota",
            "dependencies": {
                "workspace": {
                    "name": workspace,
                    "type": "workspace",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "addon": {
                    "name": addon,
                    "type": "addon",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "appinfra": {
                    "name": appinfra,
                    "type": "appinfra",
                    "product": "cloudport",
                    "cloud": "iota",
                    "relation": "parent",
                },
                "appstack": {
                    "name": appstack,
                    "type": "appstack",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "plsh": {
                    "name": plsh,
                    "type": "plsh",
                    "product": "cloudport",
                    "cloud": "iota",
                }
            },
            "configs": {
                "topology": {
                    "package_path": "topology",
                    "cue": None,
                    "path": "topology.yaml",
                    "extension": ".yaml",
                },
                "chart": {
                    "package_path": "chart",
                    "cue": None,
                    "path": "chart.yaml",
                    "extension": ".yaml",
                },
                "tarang_config": {
                    "package_path": "tarang_config",
                    "cue": None,
                    "path": "tarang_config.json",
                    "extension": ".json"
                }
            },
        }
    elif int(cp_release_first3_char) <= 315:
        data = {
            "cloud_kind": "aws",
            "cloud_name": "iota",
            "dependencies": {
                "workspace": {
                    "name": workspace,
                    "type": "workspace",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "addon": {
                    "name": addon,
                    "type": "addon",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "appinfra": {
                    "name": appinfra,
                    "type": "appinfra",
                    "product": "cloudport",
                    "cloud": "iota",
                    "relation": "parent",
                },
                "appstack": {
                    "name": appstack,
                    "type": "appstack",
                    "product": "cloudport",
                    "cloud": "iota",
                }
            },
            "configs": {
                "topology": {
                    "package_path": "topology",
                    "cue": None,
                    "path": "topology.yaml",
                    "extension": ".yaml",
                },
                "chart": {
                    "package_path": "chart",
                    "cue": None,
                    "path": "chart.yaml",
                    "extension": ".yaml",
                }
            }
        }
    else:
        data = {
            "cloud_kind": "aws",
            "cloud_name": "iota",
            "dependencies": {
                "workspace": {
                    "name": workspace,
                    "type": "workspace",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "addon": {
                    "name": addon,
                    "type": "addon",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "appinfra": {
                    "name": appinfra,
                    "type": "appinfra",
                    "product": "cloudport",
                    "cloud": "iota",
                    "relation": "parent",
                },
                "appstack": {
                    "name": appstack,
                    "type": "appstack",
                    "product": "cloudport",
                    "cloud": "iota",
                },
                "plsh": {
                    "name": plsh,
                    "type": "plsh",
                    "product": "cloudport",
                    "cloud": "iota",
                }
            },
            "configs": {
                "topology": {
                    "package_path": "topology",
                    "cue": None,
                    "path": "topology.yaml",
                    "extension": ".yaml",
                },
                "chart": {
                    "package_path": "chart",
                    "cue": None,
                    "path": "chart.yaml",
                    "extension": ".yaml",
                }
            }
        }

    # Write the data to a YAML file
    with open(yml_file_name, 'w') as file:
        yaml.dump(data, file, default_flow_style=False)

    print(f"YAML file '{yml_file_name}' created successfully!")
    try:
        if not os.path.exists(topology_folder):

            mkdir_cmd = f"""
            mkdir {topology_folder}
            """
            mkdir_res = subprocess.run(mkdir_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            print(mkdir_res)
        else:
            rm_cmd = f"""
            rm -r {topology_folder}
            """
            rm_res = subprocess.run(rm_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            mkdir_cmd = f"""
            mkdir {topology_folder}
            """
            mkdir_res = subprocess.run(mkdir_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            print(mkdir_res)
    except Exception as e:
        print(f"ERROR{e}")
    command = f"""
    cp {yml_file_name} {topology_folder}
    """
    cp_res = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    print(f"cp_res: {cp_res}")

    if int(cp_release_first3_char) >= 417: 
        trng_command = f"""
        touch tarang_config.json;cp tarang_config.json {topology_folder}
        """
        cp_trng_res = subprocess.run(trng_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        print(cp_trng_res)
    chart_command = f"""
    touch chart.yaml;cp chart.yaml {topology_folder}
    """
    cp_chrt_res = subprocess.run(chart_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    print(cp_chrt_res)
        

def update_playout_with_new_release(player_name,cp_release,dir_name):
    namespace = player_name.split(",")[0]
    feed_id = player_name.split(",")[1]
    from_headend_id = player_name.split(",")[2]
    try:
        player_release = player_name.split(",")[3]
    except:
        print("No player release is provided, skipping player rls upgrade")
    print(dir_name)
    
    clean_char = re.sub(r"\D", "", cp_release)
    cp_in_number = str(clean_char)

    cp_release_first3_char = str(cp_in_number[:3])
    
    print(f"Updating player {BLUE_START}{namespace}_{feed_id}_{from_headend_id}{BLUE_END} to {BLUE_START}{player_release}{BLUE_END}\n")
    
    check_playout = f"""
    ./amgctl cp app playout list|grep -i "{namespace}_{feed_id}_{from_headend_id}"
    """
    playout_exist_logs = subprocess.run(check_playout, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    plr_clean_output = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', playout_exist_logs.stdout)
    print(f"plr_cln_out::{plr_clean_output}")
    match = re.search(r'"name":\s*"([^"]+)"', plr_clean_output)
    print(match)
    pwd = os.getcwd()
    topology_folder = f"{pwd}/{namespace}_{feed_id}_{from_headend_id}_test"
    topology_file = f"{pwd}/{namespace}_{feed_id}_{from_headend_id}_test/topology.yaml"
    coreservice_file = f"{pwd}/{namespace}_{feed_id}_{from_headend_id}_test/coreservice.yaml"
    if player_name[:-3] == "new":
        if match:
            name_value = match.group(1)  # Extract the value inside quotes
            print(f"name_val::{name_value}")
            try:
                if os.path.exists(topology_folder) and os.path.isdir(topology_folder):
                    rm_cmd = f"""
                    rm -r {topology_folder}
                    """
                    rm_result = subprocess.run(rm_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                print(f"rm_result: {rm_result}")
            except Exception as e:
                print("cannot delete the director")
            workspace = ""
            addon = ""
            appinfra = ""
            appstack = ""
            if namespace == "sigmalh":
                workspace = "use1-onecp"
                addon = "aws-use1-onecp-use1"
                appinfra = str(namespace)
                appstack = "aws-use1-onecp-use1"
            elif namespace == "sigma8":
                workspace = "dev-onecp"
                addon = "aws-aps1-iota-onecp"
                appinfra = str(namespace)
                appstack = "aws-aps1-iota-onecp"
            plsh = f"{namespace}@{feed_id}"
            EnableS3Playout = '0'
            if s3_playout == "true":
                EnableS3Playout = '1'
            else:
                EnableS3Playout = '2'

            prefill_topology_folder = f'{pwd}/{namespace}_playout_{feed_id}_topology/'
            print(f"getting playout from {from_headend_id}...")

            get_cmd = f"""
            ./amgctl cp app playout get -n {namespace}_{feed_id}_{from_headend_id} -e {topology_folder}
            """
            get_res = subprocess.run(get_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            print(get_res)
            
            create_n_cp_prefill_playout_coreservice("coreservice.yaml",prefill_topology_folder,workspace,addon,appinfra,appstack,plsh,cp_release_first3_char)
            
            from_headend_id = f"{from_headend_id}"
            update_yaml_key(topology_file, 'params.player.deployment_config.headendId', str(from_headend_id))
            update_yaml_key(topology_file, 'params.player.ops_config.business_config.DDFeedDisplayTitle', feed_id+"_"+from_headend_id)
            update_yaml_key(topology_file, 'params.player.ops_config.business_config.OutputAR', '16:9')
            if player_release:
                update_yaml_key(topology_file, 'params.player.deployment_config.playerImage', 'cp/playout/mimas:'+player_release)
                update_yaml_key(topology_file, 'params.player_dd.deployment_config.doordarshanImage', 'cp/playout/mimas:'+player_release)
            update_yaml_key(coreservice_file, 'name', namespace+"_"+feed_id+"_"+from_headend_id)
            update_yaml_key(coreservice_file, 'cloud_kind','aws')
            update_yaml_key(coreservice_file, 'cloud_name','iota')
            #update_yaml_key(topology_file, 'params.player.deployment_config.capsequoEndpoint', 'capsequo://capsequo-server-uuid?mode=peer')

            cmd1 = f"""
            ./amgctl cp app playout create -r {cp_release} -i {topology_folder} --dry-run -q
            """
            dry_run = subprocess.run(cmd1, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            print(f"dry_run{dry_run}")
            log_check_string = "PR created in Github, PR number"
            update_check_string = "already exist in cloud"
            nochange_check_string = "No changes detected to commit"
            playout_name = f"{namespace}_{feed_id}_{from_headend_id}"
            cmd = f"./amgctl cp app playout logs -n {playout_name}"
            result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            
            #print(result)
            for line in str(result).split("x1b"):
                print(f"{YELLOW_START}{line}\n{YELLOW_END}")
                if update_check_string in line:
                    print(f"{RED_START}player already exist{RED_END} - {YELLOW_START}use update command instead{YELLOW_END}")
                    return
                if "FATAL" in line or nochange_check_string in line:
                    print(f"{RED_START} {line} \n returning... {RED_END}")
                    sys.exit(1)
                    return
                if log_check_string in line:
                    pr_url = str(line).split(":")[3]
                    print(f"{GREEN_START}Dry run successful, created PR:{GREEN_END} {BLUE_START}https:{pr_url[:-1]}{BLUE_END}")
                    time.sleep(60)
                    
                    install_cmd = f"""
                    ./amgctl cp app playout create -r {cp_release} -i {topology_folder} -q
                    """
                    install_res = subprocess.run(install_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                    #install_res = subprocess.run(install_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                    print(f"install_res",install_res)
                    
                    return
        else:
            print(f"{RED_START}Cannot find {RED_END}{YELLOW_START}{from_headend_id}{RED_START} in coreservice list{RED_START}\n use without -d option to create a new playout and plsh")
            return
        return    
    else:
        try:
            if os.path.exists(topology_folder) and os.path.isdir(topology_folder):
                rm_cmd = f"""
                rm -r {topology_folder}
                """
                rm_result = subprocess.run(rm_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            print(f"rm_result: {rm_result}")
        except Exception as e:
            print("cannot delete the director")
        workspace = ""
        addon = ""
        appinfra = ""
        appstack = ""
        if namespace == "sigmalh":
            workspace = "use1-onecp"
            addon = "aws-use1-onecp-use1"
            appinfra = str(namespace)
            appstack = "aws-use1-onecp-use1"
        elif namespace == "sigma8":
            workspace = "dev-onecp"
            addon = "aws-aps1-iota-onecp"
            appinfra = str(namespace)
            appstack = "aws-aps1-iota-onecp"
        plsh = f"{namespace}@{feed_id}"
        EnableS3Playout = '0'
        if s3_playout == "true":
            EnableS3Playout = '1'
        else:
            EnableS3Playout = '2'

        #prefill_topology_folder = f'{pwd}/{namespace}_playout_{feed_id}_test/'
        prefill_topology_folder = f'{pwd}/{namespace}_{feed_id}_{from_headend_id}_test'
        
        create_n_cp_prefill_playout_coreservice("coreservice.yaml",prefill_topology_folder,workspace,addon,appinfra,appstack,plsh,cp_release_first3_char)
        copy_dir_to_topology_folder = f"""
        cp -r {pwd}/{dir_name}/topology.yaml {pwd}/{namespace}_{feed_id}_{from_headend_id}_test/
        """
        cp_run = subprocess.run(copy_dir_to_topology_folder, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        print(f"cp_run\n {cp_run}")
        from_headend_id = f"{from_headend_id}"
        update_yaml_key(topology_file, 'params.player.deployment_config.headendId', str(from_headend_id))
        update_yaml_key(topology_file, 'params.player.ops_config.business_config.DDFeedDisplayTitle', feed_id+"_"+from_headend_id)
        update_yaml_key(topology_file, 'params.player.ops_config.business_config.OutputAR', '16:9')
        update_yaml_key(coreservice_file, 'name', namespace+"_"+feed_id+"_"+from_headend_id)
        if player_release:
            update_yaml_key(topology_file, 'params.player.deployment_config.playerImage', 'cp/playout/mimas:'+player_release)
            update_yaml_key(topology_file, 'params.player_dd.deployment_config.doordarshanImage', 'cp/playout/mimas:'+player_release)
        update_yaml_key(coreservice_file, 'cloud_kind','aws')
        update_yaml_key(coreservice_file, 'cloud_name','iota')
        #update_yaml_key(topology_file, 'params.player.deployment_config.capsequoEndpoint', 'capsequo://capsequo-server-uuid?mode=peer')

        cmd1 = f"""
        ./amgctl cp app playout update -r {cp_release} -i {topology_folder} --dry-run -q
        """
        dry_run = subprocess.run(cmd1, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        output = dry_run.stdout + dry_run.stderr
        print(output)
        if "FATAL" in output:
            print(f"FATAL: could not update the playout")
            print(output)
            print("Exiting...")
            sys.exit(1)
        log_check_string = "PR created in Github, PR number"
        update_check_string = "already exist in cloud"
        nochange_check_string = "No changes detected to commit"
        playout_name = f"{namespace}_{feed_id}_{from_headend_id}"
        cmd = f"./amgctl cp app playout logs -n {playout_name}"
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        
        #print(result)
        for line in str(result).split("x1b"):
            print(f"{YELLOW_START}{line}\n{YELLOW_END}")
            if update_check_string in line:
                print(f"{RED_START}player already exist{RED_END} - {YELLOW_START}use update command instead{YELLOW_END}")
                return
            if "FATAL" in line or nochange_check_string in line:
                print(f"{RED_START} {line} \n exiting... {RED_END}")
                sys.exit(1)
                return
            if log_check_string in line:
                pr_url = str(line).split(":")[3]
                print(f"{GREEN_START}Dry run successful, created PR:{GREEN_END} {BLUE_START}https:{pr_url[:-1]}{BLUE_END}")
                time.sleep(60)
                
                install_cmd = f"""
                ./amgctl cp app playout update -r {cp_release} -i {topology_folder} -q
                """
                install_res = subprocess.run(install_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                print(f"install_res",install_res)
                
                return

def get_pod_phase(dir2,namespace,pod_name):
    try:
        result = subprocess.run(
            ["kubectl", "get", "pod", pod_name, "-n", namespace,
             "--kubeconfig="+dir2+"/kubeconfig.yaml",
             "-o", "jsonpath={.status.phase}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def clear_logs(namespace,feed_id,head_end):
    player_namespace = f"{namespace}-playout"
    player_podname = f"player-{namespace}-{feed_id}-{head_end}-player-0"
    player_container = "player1"
    player_podname = f"player-{namespace}-{feed_id}-{head_end}-player-0"
    
    dd_namespace = f"{namespace}-playout"
    dd_podname = f"dd-{namespace}-{feed_id}-{head_end}-dd-0"
    dd_container = "dd1"
    dd_podname = f"dd-{namespace}-{feed_id}-{head_end}-dd-0"

    print(f"clearing logs of {namespace}_{feed_id}_{head_end}")

    dir = "/home/amagi/amgctl_upgrade"
    dir2 = "/home/amagi/lh_upgrade/k8s_player"
    print(f"Copying {YELLOW_START}clear_logs.sh{YELLOW_END} to the pod")

    print("Waiting for pod to enter 'Pending' phase...")
    PENDING_TIMEOUT = 1200   # seconds
    RUNNING_TIMEOUT = 1200
    start_time = time.time()
    while True:
        phase = get_pod_phase(dir2,player_namespace,player_podname)
        print (f"current phase is: {phase}")

        if phase == "Pending" or phase == "Failed" or phase == "Container Creating":
            print(f"Pod is now Pending.")
            break
        if time.time() - start_time > PENDING_TIMEOUT:
            print("Timeout reached while waiting for Pending state.")
            break
        time.sleep(2)
    start_time = time.time()
    print("Waiting for pod to move to 'Running' phase...")
    while True:
        phase = get_pod_phase(dir2,player_namespace,player_podname)
        print (f"current phase is: {phase}")

        if phase == "Running":
            print(f"Pod is now Running.")
            break
        if time.time() - start_time > RUNNING_TIMEOUT:
            print("Timeout reached while waiting for Running state.")
            break
        time.sleep(2)    
    
    #-------clearing logs of player----------
    subprocess.run([
        "kubectl", "cp","--kubeconfig="+dir2+"/kubeconfig.yaml", "-n", player_namespace, dir+"/clear_logs.sh",
        f"{player_namespace}/{player_podname}:/mnt/", "-c", player_container 
    ], check=True)

    print(f"Changing the permission of script to {GREEN_START}executable{GREEN_END}")
    subprocess.run([
        "kubectl", "exec", "-n", player_namespace, "--kubeconfig="+dir2+"/kubeconfig.yaml", player_podname, "-c", player_container,
        "--", "chmod", "+x", "/mnt/clear_logs.sh"
    ], check=True)

    result = subprocess.run([
        "kubectl", "exec", "-it", "-n", player_namespace, "--kubeconfig="+dir2+"/kubeconfig.yaml", player_podname, "-c", player_container,
        "--", "/mnt/clear_logs.sh"
    ], check=True, universal_newlines=True)
    
    #----status check of dd pod--------
    '''#commenting the lines as it would have already waited for some 30mins already
    start_time = time.time()
    while True:
        phase = get_pod_phase(dir2,dd_namespace,dd_podname)
        print (f"current phase is: {phase}")

        if phase == "Pending" or phase == "Failed" or phase == "Container Creating":
            print(f"Pod is now Pending.")
            break
        if time.time() - start_time > PENDING_TIMEOUT:
            print("Timeout reached while waiting for Pending state.")
            break
        time.sleep(2)
    start_time = time.time()
    '''
    print("Waiting for pod to move to 'Running' phase...")
    while True:
        phase = get_pod_phase(dir2,dd_namespace,dd_podname)
        print (f"current phase is: {phase}")

        if phase == "Running":
            print(f"Pod is now Running.")
            break
        if time.time() - start_time > RUNNING_TIMEOUT:
            print("Timeout reached while waiting for Running state.")
            break
        time.sleep(2)
    #-------clearing logs in dd------------ 
    print(f"Copying {YELLOW_START}clear_logs.sh{YELLOW_END} to the pod")
    subprocess.run([
        "kubectl", "cp", "--kubeconfig="+dir2+"/kubeconfig.yaml", "-n", dd_namespace, dir+"/clear_logs.sh",
        f"{dd_namespace}/{dd_podname}:/mnt/", "-c", dd_container
    ], check=True)

    print(f"Changing the permission of script to {GREEN_START}executable{GREEN_END}")
    subprocess.run([
        "kubectl", "exec", "-n", dd_namespace,"--kubeconfig="+dir2+"/kubeconfig.yaml", dd_podname, "-c", dd_container,
        "--", "chmod", "+x", "/mnt/clear_logs.sh"
    ], check=True)

    # Run the script inside the pod
    result = subprocess.run([
        "kubectl", "exec", "-it", "-n", dd_namespace, "--kubeconfig="+dir2+"/kubeconfig.yaml", dd_podname, "-c", dd_container,
        "--", "/mnt/clear_logs.sh"
    ], check=True, universal_newlines=True)
    return

    
if __name__== "__main__":
    cp_namespace = "sigma8-playout"
    cp_podname = "player-sigma8-665-005-player-0"
    cp_container = "player1"
    api_version = "1.6.4"
    sudo_pwd = ""

    account = "sigmalh"
    namespace = "sigmalh"
    cp_release = "cp_4.18.2.5"
    #cp_release = "v0.0.6745"
    feed_id = "661"
    headend_id = "003"
    topology_template = "GENERAL_PLAYOUT_2"
    s3_playout = "false"
    bring_plsh = "false"
    dir_name = ""
    #clear_logs("sigmalh","660","001")
    
    parser = argparse.ArgumentParser(description="Process some deployment parameters.")
    #parser.add_argument("-a", type=str, help="Syntax: python3 amgctl_configure.py -a <api_version> \n example: python3 amgctl_configure.py -a 0.19.10")
    parser.add_argument("-p", type=str, help="sudo password of the current user")
    parser.add_argument("-l", type=str, help="syntax: '<account>,<feed_id>,<from_headend>,<player_release>' eg: 'sigmalh,660,001,deb_fr_rc_4.20.18.1_v0_rc_4_18_1' for -l")
    
    parser.add_argument("-d", type=str, help="syntax: '<prefill_topology_dir_name>' eg: 'sigmalh_660_009_test' for -d")

    args = parser.parse_args()

    ''' # test changing the api version
    #if args.a:
    #    api_version = args.a
    '''
    if args.p:
        sudo_pwd = args.p
        install_latest_amgctl(cp_namespace, cp_podname, cp_container, api_version, sudo_pwd)
    if args.d:
        dir_name = args.d
    if args.l:
        print(f"Updating player to {args.l}")
        if dir_name != "":
            update_playout_with_new_release(args.l,cp_release,dir_name)
        #sys.exit()
        namespace = str(args.l).split(",")[0]
        feed_id = str(args.l).split(",")[1]
        head_end = str(args.l).split(",")[2]
        #clear_logs(namespace,feed_id,head_end)
        sys.exit()
    
    if not any(vars(args).values()):
        sys.exit("Error: No arguments provided. Use -h for help.\n usage: python3 amgct_configure.py -p <sudo password> -l <account>,<feed_id>,<from_headend>,<player_release> -d <prefill_topology_dir_name>")
    
    