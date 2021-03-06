from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json

from ansible.errors import AnsibleError
from ansible.module_utils._text import to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.parsing.yaml.objects import AnsibleUnicode
from ansible.plugins.action import ActionBase

try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display
    display = Display()


class ActionModule(ActionBase):

    DEFAULT_REBOOT_TIMEOUT = 1200

    def _validate_categories(self, category_names):
        valid_categories = [
            'Application',
            'Connectors',
            'CriticalUpdates',
            'DefinitionUpdates',
            'DeveloperKits',
            'FeaturePacks',
            'Guidance',
            'SecurityUpdates',
            'ServicePacks',
            'Tools',
            'UpdateRollups',
            'Updates'
        ]
        for name in category_names:
            if name not in valid_categories:
                raise AnsibleError("Unknown category_name %s, must be one of "
                                   "(%s)" % (name, ','.join(valid_categories)))

    def _run_win_updates(self, module_args, task_vars):
        display.vvv("win_updates: running win_updates module")
        result = self._execute_module(module_name='win_updates',
                                      module_args=module_args,
                                      task_vars=task_vars,
                                      wrap_async=self._task.async_val)
        return result

    def _reboot_server(self, task_vars, reboot_timeout):
        display.vvv("win_updates: rebooting remote host after update install")
        reboot_args = {
            'reboot_timeout': reboot_timeout
        }
        reboot_result = self._run_action_plugin('win_reboot', task_vars,
                                                module_args=reboot_args)
        if reboot_result.get('failed', False):
            raise AnsibleError(reboot_result['msg'])

        display.vvv("win_updates: checking WUA is not busy with win_shell "
                    "command")
        # While this always returns False after a reboot it doesn't return a
        # value until Windows is actually ready and finished installing updates
        # This needs to run with become as WUA doesn't work over WinRM
        # Ignore connection errors as another reboot can happen
        command = "(New-Object -ComObject Microsoft.Update.Session)." \
                  "CreateUpdateInstaller().IsBusy"
        shell_module_args = {
            '_raw_params': command
        }

        # run win_shell module with become and ignore any errors in case of
        # a windows reboot during execution
        orig_become = self._play_context.become
        orig_become_method = self._play_context.become_method
        orig_become_user = self._play_context.become_user
        if orig_become is None or orig_become is False:
            self._play_context.become = True
        if orig_become_method != 'runas':
            self._play_context.become_method = 'runas'
        if orig_become_user is None or 'root':
            self._play_context.become_user = 'SYSTEM'
        try:
            shell_result = self._execute_module(module_name='win_shell',
                                                module_args=shell_module_args,
                                                task_vars=task_vars)
            display.vvv("win_updates: shell wait results: %s"
                        % json.dumps(shell_result))
        except Exception as exc:
            display.debug("win_updates: Fatal error when running shell "
                          "command, attempting to recover: %s" % to_text(exc))
        finally:
            self._play_context.become = orig_become
            self._play_context.become_method = orig_become_method
            self._play_context.become_user = orig_become_user

        display.vvv("win_updates: ensure the connection is up and running")
        # in case Windows needs to reboot again after the updates, we wait for
        # the connection to be stable again
        wait_for_result = self._run_action_plugin('wait_for_connection',
                                                  task_vars)
        if wait_for_result.get('failed', False):
            raise AnsibleError(wait_for_result['msg'])

    def _run_action_plugin(self, plugin_name, task_vars, module_args=None):
        # Create new task object and reset the args
        new_task = self._task.copy()
        new_task.args = {}

        if module_args is not None:
            for key, value in module_args.items():
                new_task.args[key] = value

        # run the action plugin and return the results
        action = self._shared_loader_obj.action_loader.get(
            plugin_name,
            task=new_task,
            connection=self._connection,
            play_context=self._play_context,
            loader=self._loader,
            templar=self._templar,
            shared_loader_obj=self._shared_loader_obj
        )
        return action.run(task_vars=task_vars)

    def _merge_dict(self, original, new):
        dict_var = original.copy()
        dict_var.update(new)
        return dict_var

    def run(self, tmp=None, task_vars=None):
        self._supports_check_mode = True
        self._supports_async = True

        result = super(ActionModule, self).run(tmp, task_vars)
        del tmp  # tmp no longer has any effect

        category_names = self._task.args.get('category_names', [
            'CriticalUpdates',
            'SecurityUpdates',
            'UpdateRollups',
        ])
        if isinstance(category_names, AnsibleUnicode):
            category_names = [cat.strip() for cat in category_names.split(",")]

        state = self._task.args.get('state', 'installed')
        reboot = self._task.args.get('reboot', False)
        reboot_timeout = self._task.args.get('reboot_timeout',
                                             self.DEFAULT_REBOOT_TIMEOUT)

        # Validate the options
        try:
            self._validate_categories(category_names)
        except AnsibleError as exc:
            result['failed'] = True
            result['msg'] = to_text(exc)
            return result

        if state not in ['installed', 'searched']:
            result['failed'] = True
            result['msg'] = "state must be either installed or searched"
            return result

        try:
            reboot = boolean(reboot)
        except TypeError as exc:
            result['failed'] = True
            result['msg'] = "cannot parse reboot as a boolean: %s" % to_text(exc)
            return result

        if not isinstance(reboot_timeout, int):
            result['failed'] = True
            result['msg'] = "reboot_timeout must be an integer"
            return result

        if reboot and self._task.async_val > 0:
            result['failed'] = True
            result['msg'] = "async is not supported for this task when " \
                            "reboot=yes"
            return result

        # Run the module
        new_module_args = self._task.args.copy()
        new_module_args.pop('reboot', None)
        new_module_args.pop('reboot_timeout', None)
        result = self._run_win_updates(new_module_args, task_vars)

        changed = result['changed']
        updates = result.get('updates', dict())
        filtered_updates = result.get('filtered_updates', dict())
        found_update_count = result.get('found_update_count', 0)
        installed_update_count = result.get('installed_update_count', 0)

        # Handle automatic reboots if the reboot flag is set
        if reboot and state == 'installed' and not \
                self._play_context.check_mode:
            previously_errored = False
            while result['installed_update_count'] > 0 or \
                    result['found_update_count'] > 0 or \
                    result['reboot_required'] is True:
                display.vvv("win_updates: check win_updates results for "
                            "automatic reboot: %s" % json.dumps(result))

                # check if the module failed, break from the loop if it
                # previously failed and return error to the user
                if result.get('failed', False):
                    if previously_errored:
                        break
                    previously_errored = True
                else:
                    previously_errored = False

                reboot_error = None
                # check if a reboot was required before installing the updates
                if result.get('msg', '') == "A reboot is required before " \
                                            "more updates can be installed":
                    reboot_error = "reboot was required before more updates " \
                                   "can be installed"

                if result.get('reboot_required', False):
                    if reboot_error is None:
                        reboot_error = "reboot was required to finalise " \
                                       "update install"
                    try:
                        changed = True
                        self._reboot_server(task_vars, reboot_timeout)
                    except AnsibleError as exc:
                        result['failed'] = True
                        result['msg'] = "Failed to reboot remote host when " \
                                        "%s: %s" \
                                        % (reboot_error, to_text(exc))
                        break

                result.pop('msg', None)
                # rerun the win_updates module after the reboot is complete
                result = self._run_win_updates(new_module_args, task_vars)

                result_updates = result.get('updates', dict())
                result_filtered_updates = result.get('filtered_updates', dict())
                updates = self._merge_dict(updates, result_updates)
                filtered_updates = self._merge_dict(filtered_updates,
                                                    result_filtered_updates)
                found_update_count += result.get('found_update_count', 0)
                installed_update_count += result.get('installed_update_count', 0)
                if result['changed']:
                    changed = True

        # finally create the return dict based on the aggregated execution
        # values if we are not in async
        if self._task.async_val == 0:
            result['changed'] = changed
            result['updates'] = updates
            result['filtered_updates'] = filtered_updates
            result['found_update_count'] = found_update_count
            result['installed_update_count'] = installed_update_count

        return result
