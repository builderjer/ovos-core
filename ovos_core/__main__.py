# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Daemon launched at startup to handle skill activities.

In this repo, you will not find an entry called mycroft-skills in the bin
directory.  The executable gets added to the bin directory when installed
(see setup.py)
"""

from ovos_config.locale import setup_locale

from ovos_bus_client import MessageBusClient
from ovos_bus_client.util.scheduler import EventScheduler
from ovos_utils.skills.api import SkillApi
from ovos_core.intent_services import IntentService
from ovos_core.skill_manager import SkillManager, on_error, on_stopping, on_ready, on_alive, on_started
from ovos_utils import wait_for_exit_signal
from ovos_utils.log import LOG, init_service_logger
from ovos_utils.process_utils import reset_sigint_handler
from ovos_workshop.skills.fallback import FallbackSkill


def main(alive_hook=on_alive, started_hook=on_started, ready_hook=on_ready,
         error_hook=on_error, stopping_hook=on_stopping, watchdog=None):
    """Create a thread that monitors the loaded skills, looking for updates

    Returns:
        SkillManager instance or None if it couldn't be initialized
    """
    init_service_logger("skills")
    reset_sigint_handler()

    setup_locale()

    # Connect this process to the Mycroft message bus
    bus = MessageBusClient()
    bus.run_in_thread()
    bus.connected_event.wait()
    _register_intent_services(bus)
    event_scheduler = EventScheduler(bus, autostart=False)
    event_scheduler.daemon = True
    event_scheduler.start()
    SkillApi.connect_bus(bus)
    skill_manager = SkillManager(bus, watchdog,
                                 alive_hook=alive_hook,
                                 started_hook=started_hook,
                                 stopping_hook=stopping_hook,
                                 ready_hook=ready_hook,
                                 error_hook=error_hook)

    skill_manager.start()

    wait_for_exit_signal()

    shutdown(skill_manager, event_scheduler)


def _register_intent_services(bus):
    """Start up the all intent services and connect them as needed.

    Args:
        bus: messagebus client to register the services on
    """
    service = IntentService(bus)
    # Register handler to trigger fallback system
    bus.on(
        'mycroft.skills.fallback',
        FallbackSkill.make_intent_failure_handler(bus)
    )
    return service


def shutdown(skill_manager, event_scheduler):
    LOG.info('Shutting down Skills service')
    if event_scheduler is not None:
        event_scheduler.shutdown()
    # Terminate all running threads that update skills
    if skill_manager is not None:
        skill_manager.stop()
        skill_manager.join()
    LOG.info('Skills service shutdown complete!')


if __name__ == "__main__":
    main()
