Goal: Need a tool to characterize players in different instance type(c8a,c7a,c6a,etc) based on the nodetaint provided by the user we have the testcases defined in db, the tool lets to deploy the sepecific config for each testcase for X number of days and for each pod inject a script that captures top logs and  tool tracs the stactus of deployed pod once the X number of day are done stop the pod and get the top detailswith min, max, avg, p_99,p_999,p_9999 percentile cpu memory for adifferent testcases and push it to S3, need a commandline based tool that runs on local laptop
that can connect to a server that runs on a remote machine that has access to DB using cli tool i can see the list of testsuits and their status, test suit is a  set of testcases for a particular nodetaint. test case will be like different resolution, AR and others mentioned in one table just edit the topology file and deploy it once the 
Key chanllenges: how to authorize to use kubctl , amgclt
cli tool can ask for kubeconfig.yaml file , aws tokens 

1. How to use charactrerization tool
   1. tool --help
      1. create a testsuite
      2. show all testsuite
         1. testsuite_id, nodetaint, STATUS, date_created, termination_date
      3. show testsuite detailed `--testsuite testsuite_id`
         1. testsuite_id, testcase_id, status, date_created, date_modified, termination_date, feed, headend, player-uu-id
      4. show testsuite report `--testsuite testsuite_id --report`
         1. if status is SUCCESS, get report from S3(charactrerization_tool/reports/<testsuite_id>/)
      5. create a testsuit
         1. create testsuite id <nodetaint_XXX>
         2. ask for nodetaint, how many days to run
         3. ask for licence_key to add to coreservice.yaml
            ```
            hooks:
                license_handler:
                    parameters:
                    LICENSE_KEY: <licence_key>
            ```
         4. ask for reference_player default hypeus2@661
            1. amgclt get this will get us all in @sample_amgctl folder
            2. using `amgctl cp app playout list` check for the headend_id say for 661 _001 and 002 is already running, in the copied folder change the headend_id to _003, this should be handlend smart if _006 exists and running but not _001 or _002 use _001 and _002 
            3. refer @scripts/new_amgctl_upgrade_playout.py for getting and creating new feed
            4. in db for each test case update the test_case table
               1. with status initialted->PENDING->RUNNING/FAILURE->TERMINATED/SUCCESS
            5. all testcase stauts is success then testsuit status will be success, else if any one in initialted it ill be inititated or pending /running
         5. Select Testcase, default all
            1. in db test_cases table
               1. test_case_id, test_case_json
               2. <id>, {"params.player.ops_config.business_config.BlipFeedResolution":1920x1080i50,... }

2. 
3. DB to store diffrent testcases topology.yaml files
4. Ask for nodetaint
5. Connect to same BLIP
6. 
7. 