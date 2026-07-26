# Network Topology

<img width="1104" height="561" alt="image" src="https://github.com/user-attachments/assets/18af559a-0275-4646-b24d-0b60d0c05997" />

This topology is very similar to a previous one I created some time ago, only this time, I want to emphasize the AI agent in deployment. 


</br>On the management computer, I created an AI agent that would automatically implement configurations in response to the user’s plain english prompt. This program also catches any errors that show up while implementing configurations and recommends possible fixes. 


</br>This agent was created using Claude (free account) and is powered by Gemini so internet access is required for this agent to work.


### Devices Used:
- Cisco IOSv 15.7 router
- Cisco IOSvL2 15.2.1 switch
- GNS3 Software
- Ubuntu Container (running on VMware machine)
- Gemini

## Configurations Performed by AI Agent

### Name Change on Router
This is the agent changing the name of the router to “CISCO-ROUTER” using the agent; the user prompt is shown in the underlined text (pardon my bad spelling):
</br><img width="651" height="508.8" alt="image" src="https://github.com/user-attachments/assets/c219a46b-01fc-4d81-8b9d-889b75ef84b8" />

Navigating over to the router, we see the router name, shown by the command prompt, was successfully changed and saved: 
</br><img width="645" height="194.4" alt="image" src="https://github.com/user-attachments/assets/9a622810-1020-4a54-87c7-76de283e247b" />


### Telent on Router
</br>I had the agent implement telnet though from a cybersecurity perspective, SSH is the preferred remote access protocol since telnet has no encryption; ; the user prompt is shown in the underlined text:
</br><img width="652.8" height="387.6" alt="image" src="https://github.com/user-attachments/assets/13ed7c90-1072-4d80-96a2-89569f8a6c1a" />

__Note: This agent will ask question if it does not have all required information to implement the changes request; in this case, the password which I entered as “msfadmin”.__

</br>This is the result of the telnet configuration being used:
</br><img width="651" height="422.4" alt="image" src="https://github.com/user-attachments/assets/456237b6-406e-4cd3-8ac4-9ad450fa9af2" />

### ACL Rules on Switch
</br>Creating an ACL to stop VLAN 1 from communicating t with VLAN 99 (management VLAN); the user prompt is shown in the underlined text:
</br><img width="641.4" height="480" alt="image" src="https://github.com/user-attachments/assets/406c0598-1ccc-4bbf-8a43-9033487bf1ec" />

__Note: The agent did not have all the information to properly implement the ACL rule so it asked me a follow up question which I answered as shown in the second line of undermined text.__

This is the results of the ACL configuration:
</br><img width="651.6" height="318" alt="image" src="https://github.com/user-attachments/assets/6030b193-3a57-4a46-a0ad-909ead911381" />

### VLAN and interface Assignment on Switch
Creating a VLAN and automatically assigning interfaces to it; the user prompt is shown in the underlined text:
</br><img width="651" height="491.4" alt="image" src="https://github.com/user-attachments/assets/63acda43-fef1-4e58-9705-76871a34786d" />

These are the results of the VLAN creation and interface assignment:
</br><img width="652.2" height="221.4" alt="image" src="https://github.com/user-attachments/assets/cc23a5cd-79a0-47b6-b91d-39bf33300f5e" />


## Problems/Difficulties
The first problem I had in making this work was having the correct python version installed. As stated before, because the GNS3 network simulation software is fairly old, a lot of the preinstalled tools such as python also come in legacy versions. So to properly import the necessary python libraries to use the Gemini API, I had to install python version 3.10 or later but this made me run into another problem and that was not having the proper “pip” version installed; “pip” is just a tool on linux to install certain packages. So after installing all these dependencies, the program worked.


## Conclusion
This agent demonstrates the capabilities of AI powered programs and better ways of performing tasks on the job. Like any automation program, AI or non, this agent is not perfect and although is proficient in catching errors, unaccounted circumstances will arise; it also doesn’t help that the GNS3 software itself is fairly old as well as the local computer it runs on.


</br>This agent, like many others, does not substitute for understanding basic IT and networking concepts. Knowledge of these areas is still required since the user must know what to tell the agent such as “configure OSPF”. If the user does not understand concepts like this, they will not be able to even use this program. Programs like this complement foundational knowledge of technical concepts.
