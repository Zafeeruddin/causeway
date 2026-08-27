--> At any given time 5 camera streams per user 
--> 3 users max configurable
--> 5 min default, 15 min max
--> At any time if user clicks on camera and start recording both HLS, and rtsp should be recorded
--> Allow to download the recorded cameras (prolly need to setup Storage Bucket) Save upto 100gb of recorded videos, start deleting the older ones give a window of this might get deleted after 60pc usage 
--> Dashboard link is there, at any given point a job that can be manually run, that figures out what cameras to be extracted, map to rtsp when user clicks on any camera and start recording it should be confugrable upto 5 mins, record it and save it in storge allow it to be downloaed both raw rtsp and annotated hls


Background: Few people from our team require to download rtsp stream which is accessible only in certain network, so we are making it accessible via tunnels, vpns etc. This is a major concern because there is sometimes network drop. Lot of scripts are being written. I will elaborate later. But other use cases is just recording/seeing live camera stream from other network. 

Steps to fetch camera streams both annotated/hls:
1. Connect to forticlient VPN -- Getting access to VM which has direct access to all cameras. eg. 10.x.x.71 is VM
2. Run a script that creates camera tunnels using ssh -L port:IP:PORT.
3. Start recording the cameras using rtsp link and mapped ports to different cameras. As many cameras so many different ports.

This is the core requirement we need it for this right now. Later we will scale it for additional option where rtsp is mapped with HLS and both get recorded at same time. That is one of the requirement.

For now

User Opens Dashboard ==> Configure VPN Requirements (Support multiple types of VPNs) eg. forticlient, globalprotect, palalto, wireguard anything else ==> Expecting VPN to be whitelisted by our main server so we test the connectivity ==> Allow user to give original details about cameras in multiple ways. single camera, multiple cameras, upload csv etc. for now lets just support rtsp ==> test the camera connectivity show it live ==> Allow user to start recording ==> Recording will be saved in backend if u want i can host opensource s3 mounted on NAS we have 10tb we can allocate 1tb atleast 

Lets get started 
You can ask more questions if you 
First give architectural diagram and flow, tech stack being used 
We will check on other things later


Keep one thing in mind, its not necessary that cameras are must be forwarded. its toally possible u may need to skip vpn setup and where we deploy this app, it already has the access
Secondly we can segreagate based on teams, give access to different users by admin to different teams. eg. acme requires its own so users in acme can only seen I expect basic rbac
We will be hosting this one decently resource available machine.
We also have NAS let me know if u need to host on s3 (I will set it up locally leave it upto me) u can suggest because this s3 will be used org wide
