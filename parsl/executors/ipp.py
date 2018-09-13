import logging
import os
import uuid

from ipyparallel import Client
from libsubmit.providers import LocalProvider
from libsubmit.utils import RepresentationMixin

from parsl.dataflow.error import ConfigurationError
from parsl.executors.base import ParslExecutor
from parsl.executors.errors import *
from parsl.executors.ipp_controller import Controller
from parsl.utils import wait_for_file

logger = logging.getLogger(__name__)


class IPyParallelExecutor(ParslExecutor, RepresentationMixin):
    """The IPython Parallel executor.

    This executor uses IPythonParallel's pilot execution system to manage multiple processes
    running locally or remotely.

    Parameters
    ----------
    provider : :class:`~libsubmit.providers.provider_base.ExecutionProvider`
        Provider to access computation resources. Can be one of :class:`~libsubmit.providers.aws.aws.EC2Provider`,
        :class:`~libsubmit.providers.azureProvider.azureProvider.AzureProvider`,
        :class:`~libsubmit.providers.cobalt.cobalt.Cobalt`,
        :class:`~libsubmit.providers.condor.condor.Condor`,
        :class:`~libsubmit.providers.googlecloud.googlecloud.GoogleCloud`,
        :class:`~libsubmit.providers.gridEngine.gridEngine.GridEngine`,
        :class:`~libsubmit.providers.jetstream.jetstream.Jetstream`,
        :class:`~libsubmit.providers.local.local.Local`,
        :class:`~libsubmit.providers.sge.sge.GridEngine`,
        :class:`~libsubmit.providers.slurm.slurm.Slurm`, or
        :class:`~libsubmit.providers.torque.torque.Torque`.
    label : str
        Label for this executor instance.
    controller : :class:`~parsl.executors.ipp_controller.Controller`
        Which Controller instance to use. Default is `Controller()`.
    container_image : str
        Launch tasks in a container using this docker image. If set to None, no container is used.
        Default is None.
    engine_file : str
        Path to json engine file that will be used to compose ipp launch commands at
        scaling events. Default is '~/.ipython/profile_default/security/ipcontroller-engine.json'.
    engine_dir : str
        Alternative to above, specify the engine_dir
    working_dir : str
        Directory where input data should be staged to.
    storage_access : list of :class:`~parsl.data_provider.scheme.Scheme`
        Specifications for accessing data this executor remotely. Multiple `Scheme`s are not yet supported.
    managed : bool
        If True, parsl will control dynamic scaling of this executor, and be responsible. Otherwise,
        this is managed by the user.
    engine_debug_level : int | str
        Sets engine logging to specified debug level. Choices: (0, 10, 20, 30, 40, 50, 'DEBUG', 'INFO', 'WARN', 'ERROR', 'CRITICAL')

    .. note::
           Some deficiencies with this executor are:

               1. Ipengine's execute one task at a time. This means one engine per core
                  is necessary to exploit the full parallelism of a node.
               2. No notion of remaining walltime.
               3. Lack of throttling means tasks could be queued up on a worker.
    """

    def __init__(self,
                 provider=LocalProvider(),
                 label='ipp',
                 engine_file='~/.ipython/profile_default/security/ipcontroller-engine.json',
                 engine_dir='.',
                 working_dir=None,
                 controller=Controller(),
                 container_image=None,
                 storage_access=None,
                 engine_debug_level=None,
                 managed=True):
        self.provider = provider
        self.label = label
        self.engine_file = engine_file
        self.engine_dir = engine_dir
        self.working_dir = working_dir
        self.controller = controller
        self.engine_debug_level = engine_debug_level
        self.container_image = container_image
        self.storage_access = storage_access if storage_access is not None else []
        if len(self.storage_access) > 1:
            raise ConfigurationError('Multiple storage access schemes are not yet supported')
        self.managed = managed

        self.debug_option = ""
        if self.engine_debug_level:
            self.debug_option = "--log-level={}".format(self.engine_debug_level)

    def start(self):
        self.controller.profile = self.label
        self.controller.ipython_dir = self.run_dir
        self.controller.start()

        self.engine_file = self.controller.engine_file
        self.killed_engines = set()

        with wait_for_file(self.controller.client_file, seconds=120):
            logger.debug("Waiting for {0}".format(self.controller.client_file))

        if not os.path.exists(self.controller.client_file):
            raise Exception("Controller client file is missing at {0}".format(self.controller.client_file))

        command_composer = self.compose_launch_cmd

        self.executor = Client(url_file=self.controller.client_file)
        if self.container_image:
            command_composer = self.compose_containerized_launch_cmd
            logger.info("Launching IPP with Docker:{0}".format(self.container_image))

        self.launch_cmd = command_composer(self.engine_file, self.engine_dir, self.container_image)
        self.engines = []

        if self.provider:
            self._scaling_enabled = self.provider.scaling_enabled
            logger.debug("Starting IPyParallelExecutor with provider:\n%s", self.provider)
            if hasattr(self.provider, 'init_blocks'):
                try:
                    for i in range(self.provider.init_blocks):
                        engine = self.provider.submit(self.launch_cmd, 1)
                        logger.debug("Launched block: {0}:{1}".format(i, engine))
                        if not engine:
                            raise(ScalingFailed(self.provider.label,
                                                "Attempts to provision nodes via provider has failed"))
                        self.engines.extend([engine])

                except Exception as e:
                    logger.error("Scaling out failed: %s" % e)
                    raise e

        else:
            self._scaling_enabled = False
            logger.debug("Starting IpyParallelExecutor with no provider")

        self.lb_view = self.executor.load_balanced_view()
        logger.debug("Starting executor")

    def compose_launch_cmd(self, filepath, engine_dir, container_image):
        """Reads the json contents from filepath and uses that to compose the engine launch command.

        Args:
            filepath: Path to the engine file
            engine_dir: CWD for the engines

        """
        self.engine_file = os.path.expanduser(filepath)
        uid = str(uuid.uuid4())
        engine_json = None
        try:
            with open(self.engine_file, 'r') as f:
                engine_json = f.read()

        except OSError as e:
            logger.error("Could not open engine_json : ", self.engine_file)
            raise e

        return """mkdir -p {0}
cd {0}
cat <<EOF > ipengine.{uid}.json
{1}
EOF

mkdir -p '.ipengine_logs'
ipengine --file=ipengine.{uid}.json {debug_option} >> .ipengine_logs/$JOBNAME.log 2>&1
""".format(engine_dir, engine_json, debug_option=self.debug_option, uid=uid)

    def compose_containerized_launch_cmd(self, filepath, engine_dir, container_image):
        """Reads the json contents from filepath and uses that to compose the engine launch command.

        Notes: Add this to the ipengine launch for debug logs :
                          --log-to-file --debug
        Args:
            filepath (str): Path to the engine file
            engine_dir (str): CWD for the engines .
            container_image (str): The container to be used to launch workers
        """
        self.engine_file = os.path.expanduser(filepath)
        uid = str(uuid.uuid4())
        engine_json = None
        try:
            with open(self.engine_file, 'r') as f:
                engine_json = f.read()

        except OSError as e:
            logger.error("Could not open engine_json : ", self.engine_file)
            raise e

        return """mkdir -p {0}
cd {0}
cat <<EOF > ipengine.{uid}.json
{1}
EOF

DOCKER_ID=$(docker create --network host {2} ipengine --file=/tmp/ipengine.{uid}.json) {debug_option}
docker cp ipengine.{uid}.json $DOCKER_ID:/tmp/ipengine.{uid}.json

# Copy current dir to the working directory
DOCKER_CWD=$(docker image inspect --format='{{{{.Config.WorkingDir}}}}' {2})
docker cp -a . $DOCKER_ID:$DOCKER_CWD
docker start $DOCKER_ID

at_exit() {{
  echo "Caught SIGTERM/SIGINT signal!"
  docker stop $DOCKER_ID
}}

trap at_exit SIGTERM SIGINT
sleep infinity
""".format(engine_dir, engine_json, container_image, debug_option=self.debug_option, uid=uid)

    @property
    def scaling_enabled(self):
        return self._scaling_enabled

    def submit(self, *args, **kwargs):
        """Submits work to the thread pool.

        This method is simply pass through and behaves like a submit call as described
        here `Python docs: <https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.ThreadPoolExecutor>`_

        Returns:
              Future
        """
        return self.lb_view.apply_async(*args, **kwargs)

    def scale_out(self, *args, **kwargs):
        """Scales out the number of active workers by 1.

        This method is notImplemented for threads and will raise the error if called.

        """
        if self.provider:
            r = self.provider.submit(self.launch_cmd, *args, **kwargs)
            self.engines.extend([r])
        else:
            logger.error("No execution provider available")
            r = None

        return r

    def scale_in(self, blocks):
        """Scale in the number of active workers by ``blocks``.

        """
        if not self.provider:
            logger.error('No execution provider available')
            return None

        # Calculate number of engines
        n_engines = blocks * self.provider.nodes_per_block * self.provider.tasks_per_node
        logger.debug('Scaling in by {} blocks | {} engines'.format(blocks, n_engines))

        # Get Client queue status
        queue_status = self.executor.queue_status()

        # Get engine status, and those engines with no assigned task
        all_engines = {k for k in queue_status.keys() if k != 'unassigned' and k not in self.killed_engines}
        idle_engines = [
            id_
            for id_ in all_engines
            if queue_status[id_]['tasks'] == 0
        ]
        logger.debug('Idle engines: {}'.format(idle_engines))

        # Select engines to kill
        busy_engines = list(all_engines - set(idle_engines))
        spill_over = max(0, n_engines - len(idle_engines))
        if spill_over:
            logger.warning('Requested to scale in more engines than there are idle: '
                           '{} idle engines, killing {}'.format(len(idle_engines),
                                                               len(idle_engines) + spill_over))
        to_kill = idle_engines[:blocks] + busy_engines[:spill_over]
        logger.debug('Killing engines: {}'.format(to_kill))
        self.killed_engines |= set(to_kill)
        return self.executor.shutdown(targets=to_kill)

    def status(self):
        """Returns the status of the executor via probing the execution providers."""
        if self.provider:
            status = self.provider.status(self.engines)

        else:
            status = []

        return status

    def shutdown(self, hub=True, targets='all', block=False):
        """Shutdown the executor, including all workers and controllers.

        The interface documentation for IPP is `here <http://ipyparallel.readthedocs.io/en/latest/api/ipyparallel.html#ipyparallel.Client.shutdown>`_

        Kwargs:
            - hub (Bool): Whether the hub should be shutdown, Default:True,
            - targets (list of ints| 'all'): List of engine id's to kill, Default:'all'
            - block (Bool): To block for confirmations or not

        Raises:
             NotImplementedError
        """
        if self.controller:
            logger.debug("IPP:Shutdown sequence: Attempting controller kill")
            self.controller.close()

        # We do not actually do executor.shutdown because
        # this blocks even when requested to not block, killing the
        # controller is more effective although impolite.
        # x = self.executor.shutdown(targets=targets,
        #                           hub=hub,
        #                           block=block)

        logger.debug("Done with executor shutdown")
        return True


if __name__ == "__main__":

    pool1_config = {"poolname": "pool1",
                    "queue": "foo"}
