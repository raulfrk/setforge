_setforge_completion() {
    local IFS=$'
'
    COMPREPLY=( $( env COMP_WORDS="${COMP_WORDS[*]}" \
                   COMP_CWORD=$COMP_CWORD \
                   _SETFORGE_COMPLETE=complete_bash $1 ) )
    return 0
}

complete -o default -F _setforge_completion setforge
