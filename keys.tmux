unbind-key -q -n BTab
bind-key -n "C-\\" if-shell -F "#{@wab_open}" { run-shell -b "tmux display-popup -c '#{client_name}' -E -w 95% -h 90% -T ' волна ' '#{@wab_open}'" } { if-shell -F "#{m:*ignore-size*,#{client_flags}}" { detach-client } { send-keys "C-\\" } }
