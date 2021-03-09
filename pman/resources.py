
import os
import logging

from flask import request, current_app as app
from flask_restful import reqparse, abort, Resource

from kubernetes.client.rest import ApiException
import docker

from .openshiftmgr import OpenShiftManager
from .swarmmgr import SwarmManager


logger = logging.getLogger(__name__)

parser = reqparse.RequestParser(bundle_errors=True)
parser.add_argument('jid', dest='jid', required=True)
parser.add_argument('cmd_args', dest='cmd_args', required=True)
parser.add_argument('cmd_path_flags', dest='cmd_path_flags')
parser.add_argument('auid', dest='auid', required=True)
parser.add_argument('number_of_workers', dest='number_of_workers', required=True,)
parser.add_argument('cpu_limit', dest='cpu_limit', required=True)
parser.add_argument('memory_limit', dest='memory_limit', required=True)
parser.add_argument('gpu_limit', dest='gpu_limit', required=True)
parser.add_argument('image', dest='image', required=True)
parser.add_argument('selfexec', dest='selfexec', required=True)
parser.add_argument('selfpath', dest='selfpath', required=True)
parser.add_argument('execshell', dest='execshell', required=True)
parser.add_argument('type', dest='type', choices=('ds', 'fs', 'ts'), required=True)


class JobList(Resource):
    """
    Resource representing the list of jobs running on the compute.
    """

    def __init__(self):
        super(JobList, self).__init__()

        # mounting points for the input and outputdir in the app's container!
        self.str_app_container_inputdir = '/share/incoming'
        self.str_app_container_outputdir = '/share/outgoing'

        self.storebase = app.config.get('STOREBASE')
        self.jobdir_prefix = app.config.get('JOBDIR_PREFIX')

        self.container_env = app.config.get('CONTAINER_ENV')
        
        self.openshiftmgr       = None
        
        self.jid = ''
        
        

    def get(self):
        return {
            'server_version': app.config.get('SERVER_VERSION'),
        }
        
    def t_delete_process_openshift(self,*args, **kwargs):
        """
        Delete job and related resources (pods & pvc) from OpenShift
        """
        jid =  status = None
        jid = self.jid
        d_json  = self.get_openshift_manager().state(jid)
        if d_json['Status'] == 'Not Found':
            status = d_json['Status']
        else:
            self.get_openshift_manager().remove_job(jid)
            self.get_openshift_manager().remove_pvc(jid)
            status = 'Job deleted successfully'
        return {
            "jid" : jid,
            "status" : status
        }

    

    def t_status_process_openshift_stateObject(self, *args, **kwargs):
        """
        Process the actual JSON container return object on service
        state.
        PRECONDITIONS:
        o   This method should only ever be called by t_status_process_openshift().
        POSTCONDITIONS:
        o   A string denoting the current state is returned.
        """

        def job_exists(jid):
            """
            Returns a bool:
                - True:     <jid> does exist
                - False:    <jid> does not exist
            """
            b_exists        = False
            try:
                job         = self.get_openshift_manager().get_job(jid)
                b_exists    = True
            except:
                b_exists    = False
            return b_exists

        def job_shutDown(d_serviceInfo):
            """
            Shut down a service
            """
            try:
                self.get_openshift_manager().remove_pvc(jid)
                self.get_openshift_manager().remove_job(jid)
            except Exception as err:
                print("Error deleting pvc/job:", err)

        d_serviceState      = None
        d_jobState          = None
        str_hitIndex        = "0"
        str_logs            = ""
        d_ret               = {}

        for k,v in kwargs.items():
            if k == 'jobState':         d_jobState      = v
            if k == 'serviceState':     d_serviceState  = v
            if k == 'hitIndex':         str_hitIndex    = v
            if k == 'logs':             str_logs        = v
        if d_serviceState:
            d_ret = self.t_status_process_state(**kwargs)
            if d_ret['removeJob']:
                str_jobRoot = d_jobState['d_ret']['%s.container' % (str_hitIndex)]['jobRoot']
                self.within.ptree.cd('/%s' % str_jobRoot)
                jid = self.within.ptree.cat('jid')
                if job_exists(jid):
                    job_shutDown(jid)
        return {
            'status':       True,
            'd_process':    d_ret
        }

    
    def t_run_process_openshift(self, compute_data):
        """
        A threaded run method specialized for handling openshift
        """

        str_cmd             = ""
        d_request           = {}
        d_meta              = {}
        d_container         = {}
        d_image             = {}

        
       

        
        #self.jid            = compute_data['jid']
        self.auid           = compute_data['auid']
        str_cmd             = compute_data['cmd_args']

        # TODO: Currently it is assumed that incoming and outgoing dir will always be the last two arg
        # It holds true for 'ds' plugins but not for 'fs' plugins.
        # Implementation to support both fs ad ds plugins should be incorporated.
        str_arr = str_cmd.split()
        incoming_dir = '/share/incoming'
        outgoing_dir = '/share/outgoing'

        if 'number_of_workers' in compute_data:
            number_of_workers = compute_data['number_of_workers']
        else:
            number_of_workers = '1'
        if 'cpu_limit' in compute_data:
            cpu_limit = compute_data['cpu_limit']
        else:
            cpu_limit = '2000m'
        if 'memory_limit' in compute_data:
            memory_limit = compute_data['memory_limit']
        else:
            memory_limit = '1024Mi'

        if 'gpu_limit' in compute_data:
            gpu_limit = compute_data['gpu_limit']
        else:
            gpu_limit = 0

        if 'image' in compute_data:
            str_targetImage             = compute_data['image']

        # Create the Persistent Volume Claim
        if os.environ.get('STORAGE_TYPE') == 'swift':
            self.get_openshift_manager().create_pvc(self.jid)

        str_cmdLine = str_cmd
        self.get_openshift_manager().schedule(str_targetImage, str_cmdLine, self.jid,
                                                  number_of_workers, cpu_limit, memory_limit, gpu_limit,
                                                  incoming_dir, outgoing_dir)

           


    def post(self):
        args = parser.parse_args()

        job_id = args.jid.lstrip('/')
        self.jid = job_id
        compute_data = {
            'cmd_args': args.cmd_args,
            'cmd_path_flags': args.cmd_path_flags,
            'auid': args.auid,
            'number_of_workers': args.number_of_workers,
            'cpu_limit': args.cpu_limit,
            'memory_limit': args.memory_limit,
            'gpu_limit': args.gpu_limit,
            'image': args.image,
            'selfexec': args.selfexec,
            'selfpath': args.selfpath,
            'execshell': args.execshell,
            'type': args.type,
        }
        if self.container_env == 'openshift':
            logs = self.t_run_process_openshift(compute_data) 
            return logs
            
        cmd = self.build_app_cmd(compute_data)
        share_dir = os.path.join(self.storebase, self.jobdir_prefix + job_id)
        job_logs = ''
        job_info = {'id': '', 'image': '', 'cmd': '', 'status': 'undefined'}

        if self.container_env == 'swarm':
            swarm_mgr = SwarmManager()
            logger.info(f'Scheduling job {job_id} on the Swarm cluster')
            try:
                service = swarm_mgr.schedule(compute_data['image'], cmd, job_id, 'none',
                                             share_dir)
            except docker.errors.APIError as e:
                logger.error(f'Error from Swarm while scheduling job {job_id}, detail: '
                             f'{str(e)}')
                abort(e.response.status_code, message=str(e))
            job_info = swarm_mgr.get_service_task_info(service)
            logger.info(f'Successful job {job_id} schedule response from Swarm: '
                        f'{job_info}')
            job_logs = swarm_mgr.get_service_logs(service)

        return {
            'jid': job_id,
            'image': job_info['image'],
            'cmd': job_info['cmd'],
            'status': job_info['status'],
            'message': job_info['message'],
            'timestamp': job_info['timestamp'],
            'containerid': job_info['containerid'],
            'exitcode': job_info['exitcode'],
            'pid': job_info['pid'],
            'logs': job_logs
        }

    def build_app_cmd(self, compute_data):
        """
        Build and return the app's cmd string.
        """
        cmd_args = compute_data['cmd_args']
        cmd_path_flags = compute_data['cmd_path_flags']
        if cmd_path_flags:
            # process the argument of any cmd flag that is a 'path'
            path_flags = cmd_path_flags.split(',')
            args = cmd_args.split()
            for i in range(len(args) - 1):
                if args[i] in path_flags:
                    # each flag value is a string of one or more paths separated by comma
                    # paths = args[i+1].split(',')
                    # base_inputdir = self.str_app_container_inputdir
                    # paths = [os.path.join(base_inputdir, p.lstrip('/')) for p in paths]
                    # args[i+1] = ','.join(paths)

                    # the next is tmp until CUBE's assumptions about inputdir and path
                    # parameters are removed
                    args[i+1] = self.str_app_container_inputdir
            cmd_args = ' '.join(args)
        selfpath = compute_data['selfpath']
        selfexec = compute_data['selfexec']
        execshell = compute_data['execshell']
        type = compute_data['type']
        outputdir = self.str_app_container_outputdir
        exec = os.path.join(selfpath, selfexec)
        cmd = f'{execshell} {exec}'
        if type == 'ds':
            inputdir = self.str_app_container_inputdir
            cmd = cmd + f' {cmd_args} {inputdir} {outputdir}'
        elif type in ('fs', 'ts'):
            cmd = cmd + f' {cmd_args} {outputdir}'
        return cmd


class Job(Resource):
    """
    Resource representing a single job running on the compute.
    """
    def get_openshift_manager(self):
        self.openshiftmgr = OpenShiftManager()
        return self.openshiftmgr
        
        
    def t_status_process_openshift(self, jid):
        """
        Determine the status of a job scheduled using the openshift manager.
        PRECONDITIONS:
        o   Only call this method if a container structure exists
            in the relevant job tree!
        POSTCONDITIONS:
        o   If the job is completed, then shutdown the container cluster
            service.
        """
        
        str_logs = ''
        # Get job-id from request
        #jid = self.jid

        # Query OpenShift API to get job state
        d_json  = self.get_openshift_manager().state(jid)
        
        print (d_json)
        print (jid)

        if d_json['Status']['Message'] == 'finished':
            pod_names = self.get_openshift_manager().get_pod_names_in_job(jid)
            for _, pod_name in enumerate(pod_names):
                str_logs += self.get_openshift_manager().get_job_pod_logs(pod_name, jid)
        else:
            str_logs = d_json['Status']['Message']

        status  = d_json['Status']
        currentState =  d_json['Status']['Message']

        return {
            'status':           status,
            'logs':             str_logs,
            'currentState':     [currentState]
        }
        
    def get(self, job_id):
        container_env = app.config.get('CONTAINER_ENV')
        job_logs = ''
        job_info = {'id': '', 'image': '', 'cmd': '', 'status': 'undefined'}

        if container_env == 'swarm':
            swarm_mgr = SwarmManager()
            logger.info(f'Getting job {job_id} status from the Swarm cluster')
            try:
                service = swarm_mgr.get_service(job_id)
            except docker.errors.NotFound as e:
                abort(404, message=str(e))
            except docker.errors.APIError as e:
                abort(e.response.status_code, message=str(e))
            except docker.errors.InvalidVersion as e:
                abort(400, message=str(e))
            job_info = swarm_mgr.get_service_task_info(service)
            logger.info(f'Successful job {job_id} status response from Swarm: '
                        f'{job_info}')
            job_logs = swarm_mgr.get_service_logs(service)

            if job_info['status'] in ('undefined', 'finishedWithError',
                                      'finishedSuccessfully'):
                service.remove()  # remove job from swarm cluster
                
        else:
           
            try:
                print ("Container running")
                d_containerStatus       =   self.t_status_process_openshift(job_id)
                status                  =   d_containerStatus['status']
                logs                    =   d_containerStatus['logs']
                currentState            =   d_containerStatus['currentState']
            except Exception as e:
                if isinstance(e, ApiException) and e.reason == 'Not Found':
                    status = logs = currentState = e.reason
                else:
                    raise e

            d_ret = {
                'description':   str(status),
                'l_logs':     str(logs),
                'l_status': currentState
            }
            return {
                    "d_ret":    d_ret,
                    "status":   str(currentState)
            }

        return {
            'jid': job_id,
            'image': job_info['image'],
            'cmd': job_info['cmd'],
            'status': job_info['status'],
            'message': job_info['message'],
            'timestamp': job_info['timestamp'],
            'containerid': job_info['containerid'],
            'exitcode': job_info['exitcode'],
            'pid': job_info['pid'],
            'logs': job_logs
        }
